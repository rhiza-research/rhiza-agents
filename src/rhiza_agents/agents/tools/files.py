"""File management tools for the agent.

Trust model:
  - The agent runs as the daytona user (non-root) via su-l wrapping in
    sandbox.exec_as_daytona. POSIX permissions on /skills/ (root-owned)
    enforce that the agent cannot tamper with skill scripts.
  - /workspace and /data are mountpoint-s3 volumes. The probe at
    /tmp/daytona_ownership_probe.py confirmed mountpoint-s3 rejects
    chown/chmod, so /workspace and /data are world-writable from inside
    the sandbox. Filesystem permissions cannot enforce read-only on
    those volumes; HITL approval on execute_python_code is the only
    defense for write-from-agent attempts there.
  - The agent has no direct file-write tool. Side-effecting file writes
    happen only via skill execution (run_file with /skills/<...> paths)
    or via execute_python_code (HITL-approved arbitrary code).
  - run_file is restricted to /skills/<name>/scripts/<file> paths only.
    Anything else returns an error.
  - File metadata in state["files"] is populated by the per-sandbox
    inotify daemon's drain (see sandbox.drain_inotify_journal), keyed
    by logical path with source labels assigned per-volume.

State schema for files (path-only, no content):
    state["files"] = {
        "/foo.py":              {size, modified_at, source, last_event, first_seen},
        "/data/forecast.parq":  {size, modified_at, source: "data", ...},
        ...
    }
"""

import asyncio
import logging
import shlex
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from ...credentials import redact_output
from .sandbox import (
    SANDBOX_DATA,
    SANDBOX_SKILLS_DIR,
    SANDBOX_WORKSPACE,
    _normalize_sandbox_upload_path,
    drain_inotify_journal,
    exec_skill,
    list_workspace_files,
    read_workspace_file,
    resolve_credentials_or_error,
    workspace_path,
)

logger = logging.getLogger(__name__)


def _build_uv_run_cmd(filename: str, script_args: list[str] | None) -> str:
    """Build the ``uv run ...`` shell command with each arg shell-quoted.

    Pure helper — extracted so the command-building logic can be tested
    independently of the Daytona sandbox.
    """
    if not script_args:
        return f"uv run {filename}"
    suffix = " ".join(shlex.quote(a) for a in script_args)
    return f"uv run {filename} {suffix}"


def _logical_path(abs_path: str) -> str:
    """Map an absolute sandbox path to its logical state["files"] form.

    /workspace/foo.py -> /foo.py (workspace prefix stripped — files
    panel paths are stable across volume remounts).
    /data/forecast.parquet -> /data/forecast.parquet (kept as-is — the
    /data prefix distinguishes shared-volume files from workspace files
    in a single namespace).
    """
    if abs_path.startswith(SANDBOX_WORKSPACE + "/"):
        return abs_path[len(SANDBOX_WORKSPACE) :]
    if abs_path == SANDBOX_WORKSPACE:
        return "/"
    return abs_path


def _normalize_logical_path(path: str) -> str:
    """Ensure a path has a single leading slash (logical form)."""
    if not path.startswith("/"):
        path = "/" + path
    return path


def _is_skill_path(logical_path: str) -> bool:
    """True if the logical path refers to a script under /skills/."""
    return logical_path.startswith(SANDBOX_SKILLS_DIR + "/")


def _validate_skill_path(logical_path: str) -> str | None:
    """Reject anything that is not a clean /skills/<name>/scripts/<file> path.

    Returns an error string on rejection, None if the path is acceptable.
    Refuses paths with .. components, double slashes, empty segments, or
    that don't end with a filename component below /skills/<name>/scripts/.

    The empty-skill-name case (``/skills//scripts/x``) is caught by the
    consecutive-slashes check above, not by a redundant parts[1] check.
    """
    if not logical_path.startswith(SANDBOX_SKILLS_DIR + "/"):
        return f"Path must be under {SANDBOX_SKILLS_DIR}/"
    if ".." in logical_path.split("/"):
        return "Path may not contain '..' components"
    if "//" in logical_path:
        return "Path may not contain consecutive slashes"
    parts = logical_path.lstrip("/").split("/")
    # Expected shape: skills / <name> / scripts / <file>
    if len(parts) < 4 or parts[2] != "scripts":
        return f"Path must be of the form {SANDBOX_SKILLS_DIR}/<name>/scripts/<file>"
    if parts[-1] == "":
        return "Path has empty segment"
    return None


def make_run_file(db=None):
    """Build the ``run_file`` tool, restricted to /skills/<...> paths.

    Under the zero-trust model the agent can execute trusted skill
    scripts but cannot write to disk. ``run_file`` is the only path
    for skill execution; arbitrary script paths under /workspace are
    rejected. The script bytes were installed by the rhiza-agents
    server (privileged path, root:root mode 0644), so the agent
    cannot tamper with what it executes.

    Skills run as root (via the unwrapped exec path through
    ``exec_skill``) so they can write to /data, where the agent
    cannot. This is the trusted-helper pattern: skills are vetted
    code given elevated privileges to perform the data-acquisition
    operations the agent itself isn't trusted with.
    """

    @tool
    async def run_file(
        path: str,
        script_args: list[str] | None = None,
        credentials: list[dict] | None = None,
        *,
        runtime: ToolRuntime,
    ) -> Command:
        """Execute an installed skill script by path.

        The path must be under /skills/<name>/scripts/. Anything else
        is rejected. Skills run with elevated privileges so they can
        populate /data with downloaded artifacts; the agent cannot
        write to /data directly under the trust model.

        Args:
            path: Skill script path, e.g. '/skills/ecmwf-fetch/scripts/fetch.py'.
                Paths outside /skills/<name>/scripts/ are rejected.
            script_args: Optional list of CLI arguments passed after the
                script path. Each element is passed as a single token;
                no shell interpretation.
            credentials: Optional list of materialization plans describing
                which stored secrets to make available to this run. The
                skill activation hint lists the credential names a skill
                may use; wrap them in an ``env_vars`` plan here when
                needed. The user must approve the run before any
                credential is injected. Do not print, log, or echo
                credential values from your script — the system will
                redact verbatim occurrences from output as a backstop.
        """
        logical = _normalize_logical_path(path)
        validation_error = _validate_skill_path(logical)
        if validation_error:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"run_file rejected: {validation_error}. Path was {logical!r}.",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                    ]
                }
            )

        from .sandbox import _get_or_create_sandbox, is_sandbox_available

        if not is_sandbox_available():
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content="Error: Code sandbox is not available (DAYTONA_API_KEY not set).",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )

        thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")
        materializations = credentials or []

        # Resolve credentials before touching the sandbox so a bad
        # request never has any side effects.
        resolved = await resolve_credentials_or_error(db, thread_id, materializations, runtime.tool_call_id)
        if isinstance(resolved, Command):
            return resolved
        env_vars, file_uploads, redaction_list = resolved

        def _run():
            sandbox = _get_or_create_sandbox(thread_id)

            # Verify the skill script exists. Skills are installed by
            # the activation handler (privileged path) before run_file
            # is invoked. If missing here, either (a) the skill wasn't
            # activated, or (b) the agent passed a path that points to
            # a non-existent file.
            check = sandbox.process.exec(f"test -f {shlex.quote(logical)}")
            if check.exit_code != 0:
                return f"Error: Skill script not found: {logical}", {}

            # Apply credential file materializations. fs.upload_file
            # goes through toolbox-api as root — files land owned root.
            # Skills run as root too (exec_skill is unwrapped) so they
            # can read root-owned files at mode 0600 directly. No chown
            # to daytona is needed here; this is the run-as-root path.
            for cred_path, content in file_uploads.items():
                upload_path = _normalize_sandbox_upload_path(cred_path)
                try:
                    sandbox.fs.upload_file(content.encode("utf-8"), upload_path)
                except Exception:
                    logger.warning("Failed to upload credential file %s", cred_path, exc_info=True)
                try:
                    sandbox.process.exec(f"chmod 0600 {shlex.quote(cred_path)}")
                except Exception:
                    pass

            # Drain any pre-existing inotify events so this run's
            # session-file delta starts clean.
            drain_inotify_journal(sandbox, default_source="output")

            # Skills run as root via the unwrapped exec_skill path.
            # cwd defaults to /workspace so output files default to
            # the persistent per-conversation working area.
            response = exec_skill(
                sandbox,
                logical,
                script_args=script_args,
                cwd=SANDBOX_WORKSPACE,
                env=dict(env_vars) if env_vars else None,
            )

            # Capture new files via the inotify journal. Source
            # defaults to "output" for /workspace paths; /data paths
            # are auto-relabeled to "data" inside the drain.
            new_files = drain_inotify_journal(sandbox, default_source="output")

            # Best-effort cleanup of credential files. Run as root
            # via the unwrapped exec path.
            for cred_path in file_uploads:
                try:
                    sandbox.process.exec(f"rm -f {shlex.quote(cred_path)}")
                except Exception:
                    pass

            if response.exit_code != 0:
                return f"Error (exit code {response.exit_code}):\n{response.result}", new_files
            return response.result, new_files

        result, new_files = await asyncio.to_thread(_run)

        # Backstop: scrub verbatim secret values from anything we return.
        result = redact_output(result, redaction_list)

        update_dict: dict = {
            "messages": [
                ToolMessage(
                    content=f"Execution output for {logical}:\n{result}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }

        if new_files:
            update_dict["files"] = new_files

        return Command(update=update_dict)

    return run_file


# Public helper exposed for the file-viewer endpoint to read a logical path
# from the sandbox without needing to know about workspace_path/abs_path.
async def fetch_file_content(
    thread_id: str, logical_path: str, legacy_fallback: bytes | None = None
) -> tuple[bytes, str]:
    """Fetch a logical file path's content from the sandbox.

    Returns (content_bytes, modified_at_iso). Lazy-creates the sandbox
    if none exists for the thread. Raises FileNotFoundError if the
    file does not exist on the sandbox filesystem and no legacy
    fallback was provided.

    ``legacy_fallback`` lets the caller migrate pre-volume state-stored
    content on first read: when the file is absent from the volume but
    state has its content, the caller passes those bytes. The function
    writes them via the privileged path (toolbox-api as root) and
    proceeds as if the file had been there.
    """
    from .sandbox import _get_or_create_sandbox, write_workspace_file

    abs_path = workspace_path(_normalize_logical_path(logical_path))

    def _fetch():
        sandbox = _get_or_create_sandbox(thread_id)

        def _stat_and_read():
            stat_cmd = f"stat -c '%s|%Y' {shlex.quote(abs_path)}"
            stat_resp = sandbox.process.exec(stat_cmd)
            if stat_resp.exit_code != 0:
                return None
            try:
                _, mtime_s = stat_resp.result.strip().split("|")
                mtime = float(mtime_s)
            except ValueError:
                return None
            content = read_workspace_file(sandbox, abs_path)
            return content, datetime.fromtimestamp(mtime, tz=UTC).isoformat()

        result = _stat_and_read()
        if result is None and legacy_fallback is not None:
            try:
                write_workspace_file(sandbox, abs_path, legacy_fallback)
                logger.info("Lazy-migrated legacy file %s to workspace volume", logical_path)
            except Exception as e:
                raise FileNotFoundError(logical_path) from e
            result = _stat_and_read()

        if result is None:
            raise FileNotFoundError(logical_path)
        return result

    return await asyncio.to_thread(_fetch)


async def list_thread_files(thread_id: str) -> list[dict]:
    """List files on the thread's workspace and shared data volumes.

    Used by the "all files" file-panel toggle. Returns logical-path
    entries with source labels matching state["files"] format, so the
    UI can render both views consistently. Includes both volumes:
      - /workspace listed under logical paths (no /workspace prefix)
      - /data listed under /data/... paths
    /skills/ is intentionally excluded — skill scripts are runtime
    plumbing, not user-visible content.
    """
    from .sandbox import _get_or_create_sandbox

    def _list():
        sandbox = _get_or_create_sandbox(thread_id)
        out: list[dict] = []
        # Workspace files: source label "workspace" (live-listing view
        # source — distinct from the state-tracking sources "agent" /
        # "output" / "skill" / "data" used in the session view).
        for f in list_workspace_files(sandbox, SANDBOX_WORKSPACE):
            out.append(
                {
                    "path": _logical_path(f["path"]),
                    "size": f["size"],
                    "modified_at": f["modified_at"],
                    "source": "workspace",
                }
            )
        # Data files: keep absolute /data/... path, label as "data".
        for f in list_workspace_files(sandbox, SANDBOX_DATA):
            out.append(
                {
                    "path": f["path"],
                    "size": f["size"],
                    "modified_at": f["modified_at"],
                    "source": "data",
                }
            )
        return out

    return await asyncio.to_thread(_list)
