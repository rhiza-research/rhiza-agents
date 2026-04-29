"""Daytona sandbox tool for code execution.

The ``execute_python_code`` tool is built as a factory
(``make_execute_python_code``) so it can close over the application
database. The tool needs the database to look up the conversation's
owner and resolve credential references at tool-call time.

Credential model: the LLM passes a list of materialization plans on each
call (``credentials=[{kind: env_vars, names: [...]}, {kind: file, ...}]``).
Each plan tells the system which stored secret names to inject and how.
The system validates that every referenced name exists in the user's
store, then the existing HITL approval middleware interrupts so the
user can review the code AND the credential names before anything runs.
On approval, decrypted secret values are injected into the sandbox via
``CodeRunParams.env`` (env vars) or ``fs.upload_file`` (files), the code
runs, and verbatim secret values are scrubbed from output as a backstop.
"""

import asyncio
import base64
import logging
import os
import shlex
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from ...credentials import (
    decrypt_value,
    extract_placeholders,
    redact_output,
    substitute_placeholders,
)

logger = logging.getLogger(__name__)

# Daytona's filesystem API (``sandbox.fs.upload_file``) treats paths as
# relative to the sandbox's working directory (which is the user's home,
# /root, by default). Passing ``~/.netrc`` literally creates a directory
# named ``~``; absolute paths like ``/root/.netrc`` end up under
# ``<cwd>/root/.netrc``. Normalize credential file paths through this
# helper so they land where the script expects to find them.
#
# Shell commands like ``chmod`` and ``rm`` go through ``sandbox.process.exec``
# which runs in a shell that expands ``~`` correctly, so they keep using the
# original logical path.
_SANDBOX_HOME = "/root"

# Per-thread persistent working directory. Mounted via the homes volume
# with subpath=<thread_id> so files survive sandbox idle cleanup.
SANDBOX_WORKSPACE = "/workspace"

# Non-root user the agent's tool calls execute as. Created in the image
# via `useradd`, then every agent-driven `process.exec` is wrapped in
# `su -l daytona -c '<cmd>'` so the agent cannot escalate to root.
# `os_user="daytona"` is also passed at sandbox creation for metadata
# correctness, but it does not enforce per-command identity (Daytona open
# issue #4309) — the wrap is what actually enforces it.
SANDBOX_DAYTONA_USER = "daytona"
SANDBOX_DAYTONA_HOME = "/home/daytona"

# Trusted-skill directory. Lives on the regular container filesystem
# (NOT a volume), so POSIX permissions actually enforce: owned root:root
# mode 0644 means daytona can read+execute but not modify. Skill scripts
# are installed here at activation time via the privileged path
# (sandbox.fs.upload_file → toolbox-api as root, then chmod 0644).
SANDBOX_SKILLS_DIR = "/skills"

# Inotify journal: where the per-sandbox daemon records file events
# (create / modify / open / access / close_write) on /workspace and
# /data. Drained after every agent tool call to populate state["files"]
# with paths the conversation touched. Owned by daytona so the agent
# user can read it; the daemon itself runs as daytona too (events fire
# regardless of the watching user, but running as daytona keeps the
# log file ownership consistent).
INOTIFY_JOURNAL_PATH = "/tmp/inotify.log"


def _normalize_sandbox_upload_path(path: str) -> str:
    """Convert a logical credential-file path to the form Daytona's fs.upload_file expects.

    Used only for credential files (which still live under $HOME=/root).
    User scripts and outputs go to SANDBOX_WORKSPACE via shell-based writes.
    """
    if path.startswith("~/"):
        return path[2:]
    if path.startswith(_SANDBOX_HOME + "/"):
        return path[len(_SANDBOX_HOME) + 1 :]
    if path.startswith("/"):
        return path[1:]
    return path


# Shared cross-conversation data volume. Mounted at SANDBOX_DATA via the
# DAYTONA_DATA_VOLUME env var. Files written here by scripts are visible
# to every conversation that mounts the same data volume.
SANDBOX_DATA = "/data"


def workspace_path(logical_path: str) -> str:
    """Map a logical file path to its absolute location in the sandbox.

    Logical paths starting with ``/data/`` (or exactly ``/data``) refer to
    files on the shared data volume and are returned as-is. Everything
    else maps under SANDBOX_WORKSPACE (the per-conversation persistent
    working area).

    The two-prefix scheme keeps state["files"] keys unambiguous: a file on
    the data volume is identified by its ``/data/...`` path, a file in
    the workspace by a path that does not start with ``/data/``.
    """
    if logical_path == SANDBOX_DATA or logical_path.startswith(SANDBOX_DATA + "/"):
        return logical_path
    rel = logical_path.lstrip("/")
    return f"{SANDBOX_WORKSPACE}/{rel}"


def write_workspace_file(sandbox, abs_path: str, content: bytes) -> None:
    """Write bytes to an absolute path in the sandbox.

    fs.upload_file resolves relative-to-cwd; absolute paths are not
    respected. Use shell + base64 to write to arbitrary absolute paths.
    Caller is responsible for shell-safe ``abs_path`` (this helper quotes it).
    """
    b64 = base64.b64encode(content).decode("ascii")
    quoted = shlex.quote(abs_path)
    parent = os.path.dirname(abs_path) or "/"
    cmd = f"mkdir -p {shlex.quote(parent)} && echo {shlex.quote(b64)} | base64 -d > {quoted}"
    response = sandbox.process.exec(cmd)
    if response.exit_code != 0:
        raise OSError(f"Failed to write {abs_path}: {response.result}")


def read_workspace_file(sandbox, abs_path: str) -> bytes:
    """Read bytes from an absolute path in the sandbox via shell + base64.

    fs.download_file is relative-to-cwd; this is the absolute-path equivalent.
    Raises FileNotFoundError if the path does not exist.
    """
    quoted = shlex.quote(abs_path)
    response = sandbox.process.exec(f"test -f {quoted} && base64 -w 0 {quoted}")
    if response.exit_code != 0:
        raise FileNotFoundError(abs_path)
    return base64.b64decode(response.result.strip())


def list_workspace_files(sandbox, abs_dir: str) -> list[dict]:
    """List files under an absolute directory in the sandbox.

    Returns list of {path, size, modified_at} for regular files only,
    recursively. Returns empty list if the directory does not exist or is empty.
    """
    quoted = shlex.quote(abs_dir)
    # Use find + stat, format as TSV for safe parsing
    cmd = f"test -d {quoted} || exit 0; find {quoted} -type f -printf '%p\\t%s\\t%T@\\n' 2>/dev/null"
    response = sandbox.process.exec(cmd)
    if response.exit_code != 0:
        return []
    out: list[dict] = []
    for line in response.result.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path, size_s, mtime_s = parts
        try:
            size = int(size_s)
            mtime = float(mtime_s)
        except ValueError:
            continue
        out.append(
            {
                "path": path,
                "size": size,
                "modified_at": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
            }
        )
    return out


IDLE_TIMEOUT_MINUTES = 15

# Module-level state for sandbox lifecycle management
_sandboxes: dict[str, object] = {}
_last_used: dict[str, datetime] = {}
_daytona = None


def exec_as_daytona(sandbox, command: str, cwd: str | None = None, env: dict[str, str] | None = None):
    """Run a shell command as the daytona user via `su -l`.

    Daytona's process.exec executes commands as root by default
    (Daytona's `os_user` is metadata only — open issue #4309). Wrapping
    in `su -l daytona -c '...'` is the only way to actually drop
    privileges for agent-supplied or agent-invoked code. The login shell
    (`-l`) resets PATH/HOME to daytona's defaults; `cwd` and `env` are
    injected inside the wrapped command because su strips the parent
    process's environment.

    The command is base64-encoded before being passed through `su -c`
    to avoid shell-quoting hazards (the command may contain quotes,
    backticks, $-expansions the agent supplied).
    """
    parts: list[str] = []
    if env:
        for k, v in env.items():
            parts.append(f"export {shlex.quote(k)}={shlex.quote(v)}")
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(command)
    inner = "; ".join(parts)
    b64 = base64.b64encode(inner.encode()).decode("ascii")
    wrapped = f'su -l {SANDBOX_DAYTONA_USER} -c "echo {shlex.quote(b64)} | base64 -d | sh"'
    return sandbox.process.exec(wrapped)


def exec_skill(
    sandbox,
    abs_skill_path: str,
    script_args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
):
    """Run a script under /skills/ as root via the unwrapped exec path.

    Skills are trusted code (root-owned, agent cannot modify), so they
    execute with elevated privileges — they need root to write to /data
    (mountpoint-s3 mounts are world-writable in practice, but the trust
    model is "only skills write to /data" by convention).

    Path validation: ``abs_skill_path`` must already be an absolute
    path strictly under SANDBOX_SKILLS_DIR. Caller is responsible for
    normalizing logical paths (e.g. "/skills/foo/bar.py" → absolute) and
    rejecting any path that does not satisfy the prefix check. This
    function performs a final defensive check and raises ValueError
    otherwise — defense-in-depth even though the agent has no path to
    plant a symlink in /skills/ (root-owned, daytona has no write).
    """
    if not abs_skill_path.startswith(SANDBOX_SKILLS_DIR + "/"):
        raise ValueError(f"exec_skill called with non-skill path: {abs_skill_path!r}")
    if "/../" in abs_skill_path or abs_skill_path.endswith("/.."):
        raise ValueError(f"path traversal in skill path: {abs_skill_path!r}")

    quoted_path = shlex.quote(abs_skill_path)
    if script_args:
        suffix = " ".join(shlex.quote(a) for a in script_args)
        cmd = f"uv run {quoted_path} {suffix}"
    else:
        cmd = f"uv run {quoted_path}"

    exec_kwargs: dict = {}
    if cwd:
        exec_kwargs["cwd"] = cwd
    if env:
        exec_kwargs["env"] = dict(env)
    return sandbox.process.exec(cmd, **exec_kwargs)


def start_inotify_daemon(sandbox) -> None:
    """Start an inotifywait daemon that records file events to INOTIFY_JOURNAL_PATH.

    Watches SANDBOX_WORKSPACE and SANDBOX_DATA recursively for both
    write-side events (create, modify, close_write, move, delete) and
    read-side events (open, access). The read events are necessary
    for the spec's cache-hit visibility on /data: when a previously-
    downloaded /data file gets opened by this tool call's script, the
    file shows up in the conversation's session view as something
    this run touched, even though it wasn't written. Output format is
    TSV ``<event>\\t<path>\\t<unix_timestamp>`` per event line.

    The watched trees do NOT include Python's import paths (venvs and
    the uv cache live under /root/.cache/uv and /root/.venv, outside
    the watch set), so read events fire only for files the script
    explicitly opens under /workspace or /data — exactly the signal
    we want for "files this run touched."

    Daemon redirect uses ``>>`` (O_APPEND) — every write atomically
    seeks to end-of-file, which makes the truncate-in-place pattern
    in ``drain_inotify_journal`` work correctly on subsequent drains.
    Without O_APPEND, the daemon's FD position wouldn't follow a
    truncate, and it would keep writing past offset 0 leaving sparse
    holes in the journal that drain reads couldn't see.

    Best-effort: if inotifywait is missing or the watch can't start
    (e.g. inotify limits exhausted), logs and continues without
    session tracking — agent calls still work.

    Runs as daytona (via exec_as_daytona). Inotify events fire
    based on filesystem events regardless of which user's process
    triggered them, so running as daytona vs root is purely about
    journal-file ownership; daytona is consistent with the rest of
    the per-sandbox file ownership.
    """
    journal = shlex.quote(INOTIFY_JOURNAL_PATH)
    workspace = shlex.quote(SANDBOX_WORKSPACE)
    data = shlex.quote(SANDBOX_DATA)
    # Truncate any previous journal (daemon may have left one if a
    # different conversation reused the same sandbox name — shouldn't
    # happen with our per-thread sandboxes, but defensive).
    init_cmd = (
        f": > {journal}; "
        # Start the daemon. -m monitor mode (don't exit), -r recursive,
        # -e events. --format produces TSV; --timefmt %s makes the
        # timestamp a unix epoch second so we don't have to parse a
        # human date string in the drain. >> opens with O_APPEND so
        # every write atomically seeks to EOF — required for the
        # truncate-in-place drain pattern to work without losing data.
        f"nohup inotifywait -mr "
        f"-e create -e modify -e close_write -e move -e delete -e open -e access "
        f"--format '%e\\t%w%f\\t%T' --timefmt '%s' "
        f"{workspace} {data} >> {journal} 2>&1 &"
    )
    response = exec_as_daytona(sandbox, init_cmd)
    if response.exit_code != 0:
        logger.warning(
            "Failed to start inotify daemon (session tracking disabled): %s",
            response.result,
        )


def drain_inotify_journal(sandbox, default_source: str = "agent") -> dict[str, dict]:
    """Read and truncate the inotify journal, return aggregated path metadata.

    Returns a dict keyed by logical path (state["files"] format — leading
    slash, no /workspace prefix; /data paths kept as-is). Each value is
    {size, modified_at, source, last_event, first_seen}.

    Path-to-source mapping:
      - Paths under /data → source = "data" regardless of caller.
      - Paths under /workspace → source = ``default_source`` (caller
        passes "agent" for write_file / execute_python_code, "output"
        for run_file, etc).
      - Paths under /skills/ are filtered out — skill files are
        runtime plumbing, not user-visible content. They never appear
        in state["files"].

    Drain pattern: ``cat`` then truncate-in-place. The daemon was
    started with O_APPEND (``>>``) so writes after truncate atomically
    seek to the new end-of-file at offset 0 — the daemon's FD position
    no longer matters. There's a tiny window between ``cat`` finishing
    and ``: > journal`` running where the daemon could write events
    that get truncated and lost; this is bounded by the few microseconds
    between the two shell commands and is acceptable for "did this tool
    call touch a file."

    Path metadata is collected with a single ``stat`` call passing all
    surviving paths as positional arguments, not one stat per path.
    Paths that no longer exist (e.g. a temp file deleted before the
    drain ran) are silently dropped — stat writes them to stderr (which
    we discard) and continues with the rest.
    """
    journal = shlex.quote(INOTIFY_JOURNAL_PATH)
    # cat-then-truncate. Works correctly because the daemon writes
    # with O_APPEND, so post-truncate writes seek to the new EOF (0)
    # rather than the daemon's stale FD offset.
    drain_cmd = f"if [ -f {journal} ]; then cat {journal} 2>/dev/null; : > {journal}; fi"
    response = exec_as_daytona(sandbox, drain_cmd)
    if response.exit_code != 0:
        return {}

    # Aggregate events per path.
    by_path: dict[str, dict] = {}
    for line in response.result.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        event, path, ts_s = parts
        if not path or not path.startswith("/"):
            continue
        # Filter skill-internal paths.
        if path.startswith(SANDBOX_SKILLS_DIR + "/") or path == SANDBOX_SKILLS_DIR:
            continue
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        entry = by_path.setdefault(path, {"first_seen_ts": ts, "last_ts": ts, "last_event": event})
        if ts >= entry["last_ts"]:
            entry["last_ts"] = ts
            entry["last_event"] = event

    if not by_path:
        return {}

    # Single stat call covering every surviving path. stat prints one
    # line per existing file to stdout; missing files go to stderr
    # (which we discard) and stat exits non-zero only when no args
    # could be processed at all. We always parse stdout regardless.
    quoted_paths = " ".join(shlex.quote(p) for p in by_path)
    stat_cmd = f"stat -c '%n|%s|%Y' {quoted_paths} 2>/dev/null || true"
    stat_resp = exec_as_daytona(sandbox, stat_cmd)
    stat_by_path: dict[str, tuple[int, float]] = {}
    for line in stat_resp.result.splitlines():
        # %n could in principle contain a literal '|' if a watched
        # path embedded one; rsplit handles that by treating the last
        # two '|' fields as size and mtime.
        try:
            name, size_s, mtime_s = line.rsplit("|", 2)
            stat_by_path[name] = (int(size_s), float(mtime_s))
        except ValueError:
            continue

    # Build the state["files"] entries from the intersection of
    # journal events and successful stats.
    result: dict[str, dict] = {}
    for abs_path, agg in by_path.items():
        if abs_path not in stat_by_path:
            continue  # File was deleted between event and drain.
        size, mtime = stat_by_path[abs_path]
        # Logical path: /workspace/foo → /foo, /data/bar stays /data/bar.
        if abs_path == SANDBOX_WORKSPACE or abs_path.startswith(SANDBOX_WORKSPACE + "/"):
            logical = abs_path[len(SANDBOX_WORKSPACE) :] or "/"
            source = default_source
        elif abs_path == SANDBOX_DATA or abs_path.startswith(SANDBOX_DATA + "/"):
            logical = abs_path
            source = "data"
        else:
            # Out of scope — skip events from outside watched areas.
            continue

        result[logical] = {
            "size": size,
            "modified_at": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
            "source": source,
            "last_event": agg["last_event"],
            "first_seen": datetime.fromtimestamp(agg["first_seen_ts"], tz=UTC).isoformat(),
        }
    return result


def _get_daytona():
    """Lazily initialize the Daytona client."""
    global _daytona
    if _daytona is None:
        from daytona_sdk import Daytona, DaytonaConfig

        config_kwargs = {"api_key": os.environ["DAYTONA_API_KEY"]}
        api_url = os.environ.get("DAYTONA_API_URL")
        if api_url:
            config_kwargs["api_url"] = api_url

        _daytona = Daytona(DaytonaConfig(**config_kwargs))
    return _daytona


def _cleanup_idle_sandboxes():
    """Remove sandboxes that have been idle longer than the timeout."""
    now = datetime.now(UTC)
    expired = [
        tid for tid, last_used in _last_used.items() if (now - last_used).total_seconds() > IDLE_TIMEOUT_MINUTES * 60
    ]
    for tid in expired:
        if tid in _sandboxes:
            try:
                _get_daytona().delete(_sandboxes.pop(tid))
                logger.info("Cleaned up idle sandbox for thread %s", tid)
            except Exception:
                logger.warning("Failed to delete sandbox for thread %s", tid, exc_info=True)
            _last_used.pop(tid, None)


def _patch_proxy_url(sandbox):
    """Override the toolbox proxy URL if DAYTONA_PROXY_URL is set.

    The Daytona API returns a toolboxProxyUrl that may not be reachable from
    this container (e.g. proxy.localhost). This allows overriding it.
    """
    proxy_url = os.environ.get("DAYTONA_PROXY_URL")
    if proxy_url and hasattr(sandbox, "_toolbox_api"):
        sandbox._toolbox_api._toolbox_base_url = proxy_url
        logger.info("Patched sandbox proxy URL to %s", proxy_url)


def _daytona_sandbox_resources_from_env():
    """Return ``Resources`` for sandbox creation, or ``None`` for Daytona defaults.

    ``DAYTONA_SANDBOX_DISK_GIB`` sets root disk size in gibibytes (SDK field
    ``Resources.disk``). Disk is fixed at sandbox creation; there is no
    in-place grow in the API—raise this value and new sandboxes (new
    conversations or after idle cleanup) pick it up.

    Invalid or empty values are ignored with a warning.
    """
    raw = os.environ.get("DAYTONA_SANDBOX_DISK_GIB", "").strip()
    if not raw:
        return None
    try:
        gib = int(raw, 10)
    except ValueError:
        logger.warning("Invalid DAYTONA_SANDBOX_DISK_GIB %r — using Daytona defaults", raw)
        return None
    if gib < 1:
        logger.warning("DAYTONA_SANDBOX_DISK_GIB must be >= 1 — using Daytona defaults")
        return None
    from daytona_sdk import Resources

    return Resources(disk=gib)


# Volume configuration env vars. All optional — unset means no mounts and the
# sandbox behaves as it did before persistent storage was added.
_HOMES_VOLUME_NAME_ENV = "DAYTONA_HOMES_VOLUME"
_HOMES_MOUNT_PATH_ENV = "DAYTONA_HOMES_MOUNT_PATH"
_DATA_VOLUME_NAME_ENV = "DAYTONA_DATA_VOLUME"
_DATA_MOUNT_PATH_ENV = "DAYTONA_DATA_MOUNT_PATH"


def _build_volume_mounts(thread_id: str):
    """Build the VolumeMount list for a sandbox, or None if no volumes configured.

    The homes volume is mounted at ``DAYTONA_HOMES_MOUNT_PATH`` (default
    ``/workspace``) with ``subpath=<thread_id>`` so each conversation gets
    its own persistent slice. The data volume is mounted at
    ``DAYTONA_DATA_MOUNT_PATH`` (default ``/data``) without a subpath so
    it's shared across all conversations.

    Volume lookup uses ``create=False`` so a misconfiguration fails loudly
    rather than silently creating stray volumes.
    """
    from daytona_sdk import VolumeMount

    mounts = []

    homes_name = os.environ.get(_HOMES_VOLUME_NAME_ENV, "").strip()
    if homes_name:
        homes_path = os.environ.get(_HOMES_MOUNT_PATH_ENV, "").strip() or SANDBOX_WORKSPACE
        try:
            vol = _get_daytona().volume.get(homes_name, create=False)
            mounts.append(VolumeMount(volume_id=vol.id, mount_path=homes_path, subpath=thread_id))
        except Exception:
            logger.warning(
                "Homes volume %r not found; per-thread persistent workspace disabled", homes_name, exc_info=True
            )

    data_name = os.environ.get(_DATA_VOLUME_NAME_ENV, "").strip()
    if data_name:
        data_path = os.environ.get(_DATA_MOUNT_PATH_ENV, "").strip() or "/data"
        try:
            vol = _get_daytona().volume.get(data_name, create=False)
            mounts.append(VolumeMount(volume_id=vol.id, mount_path=data_path))
        except Exception:
            logger.warning("Data volume %r not found; shared data disabled", data_name, exc_info=True)

    return mounts or None


def cleanup_thread_workspace(thread_id: str) -> None:
    """Remove a thread's subpath from the homes volume.

    Called when a conversation is deleted. The Daytona platform's volume
    API has no direct file-delete operation, so the cleanup runs as a
    shell command in a sandbox that has the homes volume mounted at this
    thread's subpath. Two paths:

      1. If the per-thread sandbox is still alive in ``_sandboxes``, run
         the ``rm`` through it. Its mount already points at the right
         volume + subpath, so this saves the ~5–15s sandbox-create cost.
         For this to fire, the caller must invoke this BEFORE
         ``cleanup_sandbox`` (which removes the entry from ``_sandboxes``).
      2. Otherwise spin up a temp sandbox just to do the rm. This handles
         the case where the active sandbox was already idle-cleaned, or
         where the conversation never had an active sandbox in this
         server process.

    Best-effort: failures are logged, never raised, since the conversation
    DB row is already gone by the time this runs.
    """
    homes_name = os.environ.get(_HOMES_VOLUME_NAME_ENV, "").strip()
    if not homes_name:
        return

    homes_path = os.environ.get(_HOMES_MOUNT_PATH_ENV, "").strip() or SANDBOX_WORKSPACE
    rm_cmd = f"find {shlex.quote(homes_path)} -mindepth 1 -delete"

    # Path 1: reuse the active per-thread sandbox if alive.
    active = _sandboxes.get(thread_id)
    if active is not None:
        try:
            response = active.process.exec(rm_cmd)
            if response.exit_code != 0:
                logger.warning(
                    "Workspace cleanup for thread %s (active sandbox) exited %d: %s",
                    thread_id,
                    response.exit_code,
                    response.result,
                )
            return
        except Exception:
            logger.warning(
                "Active-sandbox workspace cleanup failed for thread %s; falling back to temp sandbox",
                thread_id,
                exc_info=True,
            )
            # Fall through to path 2.

    # Path 2: no live sandbox (or the active path failed). Spin up a
    # temp sandbox just to do the rm.
    try:
        from daytona_sdk import CreateSandboxFromImageParams, Image, VolumeMount

        vol = _get_daytona().volume.get(homes_name, create=False)
        # Minimal image — no need for git/uv since we just rm -rf.
        image = Image.debian_slim("3.12")
        sb = _get_daytona().create(
            CreateSandboxFromImageParams(
                image=image,
                volumes=[VolumeMount(volume_id=vol.id, mount_path=homes_path, subpath=thread_id)],
            )
        )
        try:
            response = sb.process.exec(rm_cmd)
            if response.exit_code != 0:
                logger.warning(
                    "Workspace cleanup for thread %s (temp sandbox) exited %d: %s",
                    thread_id,
                    response.exit_code,
                    response.result,
                )
        finally:
            _get_daytona().delete(sb)
    except Exception:
        logger.warning("Failed to clean up workspace for thread %s", thread_id, exc_info=True)


async def cleanup_thread_workspace_async(thread_id: str) -> None:
    """Async wrapper for ``cleanup_thread_workspace``."""
    await asyncio.to_thread(cleanup_thread_workspace, thread_id)


def _build_sandbox_image():
    """Build the Daytona Image declaration for the runtime.

    - python:3.12-slim base
    - git installed (uv/pip can fetch VCS dependencies)
    - inotify-tools installed (the inotifywait binary the per-sandbox
      session-tracking daemon runs)
    - uv installed via pip
    - daytona user created (the non-root identity all agent tool calls
      execute as via su -l wrapping)
    - /workspace, /data, /skills directories created. Skills directory
      is owned root:root mode 0755 — POSIX permissions actually enforce
      here because /skills/ is on the regular container FS, not a volume.
      /workspace and /data ownership is determined by mountpoint-s3 mount
      options (probe confirmed they arrive nobody:nogroup mode 0777, and
      chown/chmod from inside is not permitted) so we don't try to chown
      them — see /tmp/daytona_ownership_probe.py for the empirical basis.
    """
    from daytona_sdk import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update && apt-get install -y --no-install-recommends git inotify-tools "
            "&& rm -rf /var/lib/apt/lists/*",
            # Non-root user the agent's tool calls execute as. -m creates
            # /home/daytona; -s sets the login shell; -r marks it a
            # system account.
            "groupadd -r daytona && useradd -r -m -g daytona -s /bin/bash daytona",
            # Mount points / regular dirs.
            f"mkdir -p {SANDBOX_WORKSPACE} /data {SANDBOX_SKILLS_DIR}",
            # /skills/ stays root:root mode 0755 — agent (daytona) reads
            # and executes but cannot write. Skill scripts dropped here at
            # activation time get chmod 0644 (also root-owned).
            f"chmod 0755 {SANDBOX_SKILLS_DIR}",
        )
        .pip_install(["uv"])
    )


def _get_or_create_sandbox(thread_id: str):
    """Get an existing sandbox for a thread or create a new one.

    Uses a declarative image with uv pre-installed so that scripts with
    PEP 723 inline metadata (# /// script) can declare their own dependencies
    and have them resolved automatically via `uv run`. Git is installed so
    ``pip install`` / ``uv pip install`` can fetch VCS dependencies (e.g.
    ``git+https://...``).

    Volume mounts are wired in based on env vars (see
    ``_build_volume_mounts``): per-thread persistent ``/workspace`` and
    shared ``/data`` when configured.

    Optional env ``DAYTONA_SANDBOX_DISK_GIB`` sets sandbox disk size (GiB);
    see ``_daytona_sandbox_resources_from_env``.
    """
    from daytona_sdk import CreateSandboxFromImageParams

    _cleanup_idle_sandboxes()

    if thread_id not in _sandboxes:
        image = _build_sandbox_image()
        resources = _daytona_sandbox_resources_from_env()
        volumes = _build_volume_mounts(thread_id)
        sandbox = _get_daytona().create(
            CreateSandboxFromImageParams(
                image=image,
                resources=resources,
                volumes=volumes,
                # Metadata only — Daytona open issue #4309 confirms
                # process.exec doesn't honor this. The actual identity
                # for agent calls comes from exec_as_daytona's su wrap.
                os_user=SANDBOX_DAYTONA_USER,
            )
        )
        _patch_proxy_url(sandbox)
        _sandboxes[thread_id] = sandbox
        # Best-effort start of the per-sandbox inotify daemon. Failure
        # leaves session-tracking off for this sandbox but doesn't
        # affect anything else.
        try:
            start_inotify_daemon(sandbox)
        except Exception:
            logger.warning("Failed to start inotify daemon for thread %s", thread_id, exc_info=True)
        logger.info(
            "Created sandbox for thread %s (resources=%r, volumes=%d)",
            thread_id,
            resources,
            len(volumes) if volumes else 0,
        )

    _last_used[thread_id] = datetime.now(UTC)
    return _sandboxes[thread_id]


def is_sandbox_available() -> bool:
    """Check if the Daytona sandbox is configured (API key is set)."""
    return bool(os.environ.get("DAYTONA_API_KEY"))


def cleanup_sandbox(thread_id: str):
    """Clean up a specific sandbox (e.g. when a conversation is deleted)."""
    if thread_id in _sandboxes:
        try:
            _get_daytona().delete(_sandboxes.pop(thread_id))
            logger.info("Cleaned up sandbox for thread %s", thread_id)
        except Exception:
            logger.warning("Failed to delete sandbox for thread %s", thread_id, exc_info=True)
        _last_used.pop(thread_id, None)


async def cleanup_idle_sandboxes():
    """Async wrapper for idle sandbox cleanup."""
    await asyncio.to_thread(_cleanup_idle_sandboxes)


def _collect_referenced_names(materializations: list[dict]) -> list[str]:
    """Walk a materialization list and collect every secret name it references.

    Both ``env_vars`` and ``file`` kinds carry an explicit ``names`` list, so
    this is just the deduplicated union of those lists in the order they
    first appear. Used both for validation (before HITL) and for redaction
    list construction (after decryption).
    """
    seen: dict[str, None] = {}
    for m in materializations or []:
        if not isinstance(m, dict):
            continue
        for n in m.get("names") or []:
            if isinstance(n, str):
                seen.setdefault(n, None)
    return list(seen.keys())


def _validate_materializations(materializations: Any) -> str | None:
    """Validate the structural shape of the credentials argument.

    Returns an error string if invalid, or None if OK. Does not check
    name existence in the user's credential store — that's a separate step
    that needs the db.

    Accepted shapes:

        {"kind": "env_vars", "names": ["NAME1", "NAME2"]}

        {"kind": "file", "path": "~/.netrc", "names": ["NAME1", "NAME2"],
         "content": "...{NAME1}...{NAME2}..."}

    For the file kind, every ``{NAME}`` placeholder in ``content`` MUST also
    appear in the explicit ``names`` list — this catches the LLM mismatching
    its placeholders against its declared accessed names.
    """
    if not isinstance(materializations, list):
        return "credentials must be a list of materialization plans"
    for i, m in enumerate(materializations):
        if not isinstance(m, dict):
            return f"credentials[{i}] must be an object"
        kind = m.get("kind")
        names = m.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            return f"credentials[{i}].names must be a non-empty list of secret names"
        if kind == "env_vars":
            pass  # names already validated
        elif kind == "file":
            path = m.get("path")
            content = m.get("content")
            if not isinstance(path, str) or not path:
                return f"credentials[{i}].path must be a non-empty string"
            if not isinstance(content, str):
                return f"credentials[{i}].content must be a string"
            placeholders = set(extract_placeholders(content))
            declared = set(names)
            missing = placeholders - declared
            if missing:
                return (
                    f"credentials[{i}].content references {sorted(missing)} but "
                    f"those names are not in credentials[{i}].names"
                )
        else:
            return f"credentials[{i}].kind must be 'env_vars' or 'file'"
    return None


async def _resolve_secrets(db, user_id: str, names: list[str]) -> tuple[dict[str, str], list[str], str | None]:
    """Decrypt every named secret available in the user's store.

    Missing names are treated as **optional**: they are returned in the
    ``missing`` list and silently dropped from injection rather than
    failing the call. The CLI/script is responsible for raising a clear
    error if it actually requires a credential that wasn't injected. This
    lets a single skill cover both public-anonymous and private-authenticated
    workflows without splitting into two skills per source.

    Returns ``(values, missing, error_string)``:
      - ``values``: name -> decrypted value for every name that was present.
      - ``missing``: names that were not in the user's store (can be empty).
      - ``error_string``: only set when decryption itself fails for a
        present-but-unreadable secret (a real error, not just absence).
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        ct = await db.get_credential_ciphertext_by_name(user_id, name)
        if ct is None:
            missing.append(name)
            continue
        try:
            values[name] = decrypt_value(ct)
        except Exception as e:  # pragma: no cover - decrypt should not fail in practice
            return values, missing, f"failed to decrypt credential {name!r}: {e}"
    return values, missing, None


def _build_runtime_injection(
    materializations: list[dict],
    secret_values: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Turn validated materializations + decrypted values into env vars and files.

    Names that are absent from ``secret_values`` (because the user did not
    have that credential stored) are silently skipped:

      - ``env_vars`` plans only set entries for names that resolved.
      - ``file`` plans are dropped entirely if any of their referenced names
        are missing — a partially-rendered netrc/credential file is worse
        than no file at all (callers expecting a complete file would pass
        broken auth to the underlying tool).

    Returns ``(env_vars, files)`` where ``env_vars`` is a flat dict for
    ``CodeRunParams.env`` and ``files`` is a dict of ``path -> content`` ready
    to upload. When two ``file`` materializations target the same path,
    their content is concatenated in the order the LLM listed them.
    """
    env: dict[str, str] = {}
    files: dict[str, str] = {}
    for m in materializations:
        if m["kind"] == "env_vars":
            for name in m["names"]:
                if name in secret_values:
                    env[name] = secret_values[name]
        elif m["kind"] == "file":
            # Skip the entire file if any referenced name is unresolved —
            # don't write a half-templated credential file to the sandbox.
            if not all(n in secret_values for n in m["names"]):
                continue
            path = m["path"]
            rendered = substitute_placeholders(m["content"], secret_values)
            if path in files:
                files[path] = files[path] + rendered
            else:
                files[path] = rendered
    return env, files


async def resolve_credentials_or_error(
    db,
    thread_id: str,
    materializations: list[dict],
    tool_call_id: str,
) -> tuple[dict[str, str], dict[str, str], list[str]] | Command:
    """Validate, resolve, and decrypt the credentials for one tool call.

    Shared by ``execute_python_code`` and ``run_file`` so both tools have
    the exact same credential semantics.

    Returns ``(env_vars, file_uploads, redaction_list)`` on success, or a
    ``Command`` carrying a tool-error message on any failure (bad shape,
    missing credential, decryption failure, etc). The caller should check
    ``isinstance(result, Command)`` and short-circuit on that branch.

    The decrypted secret values are kept only inside this function's local
    scope until they are baked into ``env_vars`` and rendered file content;
    the caller never sees the raw payload dict.
    """
    if not materializations:
        return {}, {}, []

    shape_error = _validate_materializations(materializations)
    if shape_error:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Credential error: {shape_error}",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                ]
            }
        )

    referenced_names = _collect_referenced_names(materializations)
    if not referenced_names:
        return {}, {}, []

    if db is None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Credential error: credentials feature is not configured",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                ]
            }
        )

    convo = await db.get_conversation_by_id(thread_id)
    if not convo:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Credential error: cannot resolve conversation owner",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                ]
            }
        )

    secret_values, missing, err = await _resolve_secrets(db, convo["user_id"], referenced_names)
    if err:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Credential error: {err}",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                ]
            }
        )
    # Missing names are intentionally non-fatal — see _resolve_secrets docstring.
    # The CLI/script is responsible for reporting if a credential it needed
    # wasn't injected. Logging the missing set helps debug "why didn't auth work".
    if missing:
        logger.info("Credential names not configured (skipped): %s", ", ".join(missing))

    env_vars, file_uploads = _build_runtime_injection(materializations, secret_values)
    redaction_list = list(secret_values.values())
    secret_values.clear()
    return env_vars, file_uploads, redaction_list


def make_execute_python_code(db=None):
    """Build the ``execute_python_code`` tool with credential support.

    SECURITY NOTE — residual tamper surface:

    This tool is the only agent-controlled path to arbitrary code
    execution. Under the zero-trust model:

      - The agent runs as the ``daytona`` user via ``exec_as_daytona``,
        so it cannot tamper with /skills/ (root-owned, mode 0644).
        POSIX permissions enforce skill-script integrity.
      - However, /workspace and /data are mountpoint-s3 volumes. The
        ownership probe (/tmp/daytona_ownership_probe.py) confirmed that
        chown/chmod return EPERM on those mounts even from root, and
        mountpoint-s3 reports them as world-writable regardless of the
        underlying user. **Filesystem permissions do not enforce
        read-only on /workspace or /data.** The daytona user CAN write,
        modify, or delete files there via ``execute_python_code``.
      - HITL approval is the only defense for /workspace / /data write
        attempts. Every ``execute_python_code`` invocation is in
        ``_HITL_TOOLS`` (graph.py) and the user reviews the code before
        it runs. A sufficiently subtle tamper that the user approves
        without noticing would succeed.

    The /skills/ trust story is strict (enforced by the OS); the
    /workspace / /data integrity story is HITL-mediated. If this
    tool is ever extended, removed, or replaced, revisit how skill
    outputs in /workspace are protected from agent post-processing.

    Args:
        db: Application database. Required for credential resolution.
            When None, the tool still runs but rejects any non-empty
            ``credentials`` argument.

    Returns the configured ``execute_python_code`` tool.
    """

    @tool
    async def execute_python_code(
        code: str,
        credentials: list[dict] | None = None,
        *,
        runtime: ToolRuntime,
    ) -> Command:
        """Execute Python code in a sandboxed environment and return the output.

        Use this tool to run data analysis, computations, or any Python code.
        The sandbox persists across calls within the same conversation, so
        you can build on previous code executions.

        Args:
            code: Python code to execute.
            credentials: Optional list of materialization plans describing
                which stored secrets to make available to this run. When a
                skill activation lists required credential names (from a
                ``metadata.openclaw.requires.env`` block in its SKILL.md),
                use those names here — wrap them in an ``env_vars`` plan.

                Each entry has a ``kind`` of either ``env_vars`` or ``file``,
                and an explicit ``names`` list enumerating the stored secrets
                it touches.

                ``env_vars`` entries set environment variables. The env var
                name is the same as the stored secret name:

                    {"kind": "env_vars", "names": ["TAHMO_USERNAME", "TAHMO_PASSWORD"]}

                ``file`` entries write a file with templated content. Use
                ``{NAME}`` placeholders in ``content`` to inject stored
                secret values; the same names must also appear in the
                ``names`` list:

                    {"kind": "file", "path": "~/.netrc",
                     "names": ["NASA_USERNAME", "NASA_PASSWORD"],
                     "content": "machine x login {NASA_USERNAME} password {NASA_PASSWORD}\\n"}

                The user must approve the run before any credential is
                injected. Do not print, log, or echo credential values
                from your script — the system will redact verbatim
                occurrences from output as a backstop, and the user can see
                which secret names you requested in the approval card.
        """
        thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")
        materializations = credentials or []

        # 1. Structural validation. Bad shapes get rejected with a
        #    tool-error message that the LLM can read and correct from.
        shape_error = _validate_materializations(materializations)
        if shape_error:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"Credential error: {shape_error}",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                    ]
                }
            )

        # 2. Resolve referenced names against the user's store. If any name
        #    is missing, reject before running anything.
        referenced_names = _collect_referenced_names(materializations)
        secret_values: dict[str, str] = {}
        if referenced_names:
            if db is None:
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content="Credential error: credentials feature is not configured",
                                tool_call_id=runtime.tool_call_id,
                                status="error",
                            )
                        ]
                    }
                )
            convo = await db.get_conversation_by_id(thread_id)
            if not convo:
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content="Credential error: cannot resolve conversation owner",
                                tool_call_id=runtime.tool_call_id,
                                status="error",
                            )
                        ]
                    }
                )
            user_id = convo["user_id"]
            secret_values, missing, err = await _resolve_secrets(db, user_id, referenced_names)
            if missing:
                logger.info("Credential names not configured (skipped): %s", ", ".join(missing))
            if err:
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content=f"Credential error: {err}",
                                tool_call_id=runtime.tool_call_id,
                                status="error",
                            )
                        ]
                    }
                )

        # 3. Build the per-execution env vars and file uploads from the
        #    decrypted values. Drop the values dict immediately after.
        env_vars, file_uploads = _build_runtime_injection(materializations, secret_values)
        redaction_list = list(secret_values.values())
        secret_values.clear()

        def _run():
            sandbox = _get_or_create_sandbox(thread_id)

            # Apply credential file materializations before running.
            # fs.upload_file goes through the Daytona toolbox-api as
            # root, so files land owned root:root. After upload, chown
            # to daytona and chmod 0600 so the daytona-context script
            # can read them; without the chown, daytona would EACCES
            # on a 0600 root-owned file.
            for path, content in file_uploads.items():
                upload_path = _normalize_sandbox_upload_path(path)
                try:
                    sandbox.fs.upload_file(content.encode("utf-8"), upload_path)
                except Exception:
                    logger.warning("Failed to upload credential file %s", path, exc_info=True)
                # chown + chmod via the unwrapped exec path (root) so
                # daytona ends up able to read the file.
                try:
                    sandbox.process.exec(
                        f"chown {SANDBOX_DAYTONA_USER}:{SANDBOX_DAYTONA_USER} {shlex.quote(path)}; "
                        f"chmod 0600 {shlex.quote(path)}"
                    )
                except Exception:
                    pass

            # Run the agent's code as daytona via su -l wrapping. The
            # code is base64-encoded to avoid shell-quoting hazards from
            # the agent's source (which may contain quotes, $-expansions,
            # etc.). The wrapped command pipes the decoded source into
            # python on stdin so the bytes never appear in argv (would
            # be visible in /proc/<pid>/cmdline).
            #
            # Drain any pre-existing inotify events first so this run's
            # session-file delta starts from a clean slate.
            drain_inotify_journal(sandbox, default_source="agent")

            code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
            run_cmd = f"echo {shlex.quote(code_b64)} | base64 -d | python3"
            response = exec_as_daytona(sandbox, run_cmd, env=env_vars or None)

            # Pick up any new files the script wrote, via the inotify
            # journal. The drain returns logical paths with proper
            # source labels (/data → "data", /workspace → "agent").
            new_files = drain_inotify_journal(sandbox, default_source="agent")

            # Best-effort cleanup of credential files. Run as root via
            # the unwrapped path so we can rm files we chowned to
            # daytona (root can rm regardless).
            for path in file_uploads:
                try:
                    sandbox.process.exec(f"rm -f {shlex.quote(path)}")
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
                    content=result,
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }

        if new_files:
            update_dict["files"] = new_files

        return Command(update=update_dict)

    return execute_python_code
