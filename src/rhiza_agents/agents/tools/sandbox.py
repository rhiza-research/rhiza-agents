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

# File extensions that should be stored as base64-encoded binary
# Daytona's filesystem API (``sandbox.fs.upload_file``) treats paths as
# relative to the sandbox's home directory. Passing ``~/.netrc`` literally
# creates a directory named ``~`` instead of expanding the tilde, and
# passing absolute paths like ``/root/.netrc`` ends up under
# ``<home>/root/.netrc``. Normalize all credential file paths through this
# helper so they land where the script expects to find them.
#
# Shell commands like ``chmod`` and ``rm`` go through ``sandbox.process.exec``
# which runs in a shell that expands ``~`` correctly, so they keep using the
# original logical path.
_SANDBOX_HOME = "/root"


def _normalize_sandbox_upload_path(path: str) -> str:
    """Convert a logical sandbox path to the form Daytona's fs.upload_file expects."""
    if path.startswith("~/"):
        return path[2:]
    if path.startswith(_SANDBOX_HOME + "/"):
        return path[len(_SANDBOX_HOME) + 1 :]
    if path.startswith("/"):
        return path[1:]
    return path


_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".parquet",
    ".feather",
}

IDLE_TIMEOUT_MINUTES = 15

# Module-level state for sandbox lifecycle management
_sandboxes: dict[str, object] = {}
_last_used: dict[str, datetime] = {}
_daytona = None


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


def _get_or_create_sandbox(thread_id: str):
    """Get an existing sandbox for a thread or create a new one.

    Uses a declarative image with uv pre-installed so that scripts with
    PEP 723 inline metadata (# /// script) can declare their own dependencies
    and have them resolved automatically via `uv run`. Git is installed so
    ``pip install`` / ``uv pip install`` can fetch VCS dependencies (e.g.
    ``git+https://...``).

    Optional env ``DAYTONA_SANDBOX_DISK_GIB`` sets sandbox disk size (GiB);
    see `_daytona_sandbox_resources_from_env`.
    """
    from daytona_sdk import CreateSandboxFromImageParams, Image

    _cleanup_idle_sandboxes()

    if thread_id not in _sandboxes:
        image = (
            Image.debian_slim("3.12")
            .run_commands(
                "apt-get update && apt-get install -y --no-install-recommends git "
                "&& rm -rf /var/lib/apt/lists/*"
            )
            .pip_install(["uv"])
        )
        resources = _daytona_sandbox_resources_from_env()
        sandbox = _get_daytona().create(
            CreateSandboxFromImageParams(image=image, resources=resources)
        )
        _patch_proxy_url(sandbox)
        _sandboxes[thread_id] = sandbox
        if resources is not None:
            logger.info("Created sandbox for thread %s (with uv, git, %r)", thread_id, resources)
        else:
            logger.info("Created sandbox for thread %s (with uv, git)", thread_id)

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


async def _resolve_secrets(db, user_id: str, names: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """Decrypt every named secret for a user.

    Returns ``(values, None)`` on success or ``(None, error_string)`` if any
    name is missing from the user's store. Caller must have already
    validated the materialization shape.
    """
    values: dict[str, str] = {}
    for name in names:
        ct = await db.get_credential_ciphertext_by_name(user_id, name)
        if ct is None:
            return None, f"credential {name!r} is not configured — add it in settings"
        try:
            values[name] = decrypt_value(ct)
        except Exception as e:  # pragma: no cover - decrypt should not fail in practice
            return None, f"failed to decrypt credential {name!r}: {e}"
    return values, None


def _build_runtime_injection(
    materializations: list[dict],
    secret_values: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Turn validated materializations + decrypted values into env vars and files.

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
                env[name] = secret_values[name]
        elif m["kind"] == "file":
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

    secret_values, err = await _resolve_secrets(db, convo["user_id"], referenced_names)
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

    env_vars, file_uploads = _build_runtime_injection(materializations, secret_values)
    redaction_list = list(secret_values.values())
    secret_values.clear()
    return env_vars, file_uploads, redaction_list


def make_execute_python_code(db=None):
    """Build the ``execute_python_code`` tool with credential support.

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
                which stored secrets to make available to this run. When an
                MCP tool returns a skill document with a ``requires_credentials``
                frontmatter block, copy the relevant entries from that block
                verbatim into this argument. The entries have the same shape
                in the frontmatter and in this tool argument so no
                interpretation is needed.

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
            secret_values, err = await _resolve_secrets(db, user_id, referenced_names)
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

            # Apply file materializations before running. These persist for
            # the lifetime of the sandbox; future runs that don't request
            # them will still see them in the filesystem until the sandbox
            # is recycled. Best-effort cleanup happens after the run.
            #
            # Daytona's upload_file signature is (content_bytes, remote_path),
            # NOT (remote_path, content_bytes). And remote_path is resolved
            # against the sandbox's working directory and does NOT expand
            # ``~``, so the path is normalized first.
            for path, content in file_uploads.items():
                upload_path = _normalize_sandbox_upload_path(path)
                try:
                    sandbox.fs.upload_file(content.encode("utf-8"), upload_path)
                except Exception:
                    logger.warning("Failed to upload credential file %s", path, exc_info=True)
                # netrc and similar credential files conventionally need
                # restrictive permissions; default everything to 0600. Use
                # the original logical path here — chmod runs in a shell
                # that expands ``~`` correctly.
                try:
                    sandbox.process.exec(f"chmod 600 {path}")
                except Exception:
                    pass

            # Snapshot files before execution to detect new output files
            try:
                pre_files = {f.name for f in sandbox.fs.list_files(".")}
            except Exception:
                pre_files = set()

            run_params = None
            if env_vars:
                try:
                    from daytona_sdk import CodeRunParams

                    run_params = CodeRunParams(env=dict(env_vars))
                except Exception:
                    logger.warning("Failed to build CodeRunParams; env vars will not be set", exc_info=True)

            if run_params is not None:
                response = sandbox.process.code_run(code, params=run_params)
            else:
                response = sandbox.process.code_run(code)

            # Discover new files created during execution
            new_files = {}
            try:
                post_files = sandbox.fs.list_files(".")
                for f in post_files:
                    if f.is_dir or f.name in pre_files:
                        continue
                    # Skip large files > 1MB
                    if f.size and f.size > 1_000_000:
                        continue
                    try:
                        content_bytes = sandbox.fs.download_file(f.name)
                        ext = "." + f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
                        if ext in _BINARY_EXTENSIONS:
                            new_files[f"/{f.name}"] = {
                                "content": base64.b64encode(content_bytes).decode("ascii"),
                                "encoding": "base64",
                            }
                        else:
                            new_files[f"/{f.name}"] = {
                                "content": content_bytes.decode("utf-8", errors="replace"),
                                "encoding": "utf-8",
                            }
                    except Exception:
                        pass
            except Exception:
                pass

            # Best-effort cleanup of credential files so they don't linger
            # between executions.
            for path in file_uploads:
                try:
                    sandbox.process.exec(f"rm -f {path}")
                except Exception:
                    pass

            if response.exit_code != 0:
                return f"Error (exit code {response.exit_code}):\n{response.result}", new_files
            return response.result, new_files

        result, new_files = await asyncio.to_thread(_run)

        # Backstop: scrub verbatim secret values from anything we return.
        result = redact_output(result, redaction_list)

        # Build files state update from captured output files
        now = datetime.now(UTC).isoformat()
        files_update = {}
        for fpath, finfo in new_files.items():
            encoding = finfo["encoding"]
            raw = finfo["content"]
            if encoding == "base64":
                files_update[fpath] = {
                    "content": [raw],
                    "source": "output",
                    "encoding": "base64",
                    "modified_at": now,
                }
            else:
                redacted = redact_output(raw, redaction_list)
                lines = redacted.split("\n")
                files_update[fpath] = {
                    "content": lines,
                    "source": "output",
                    "modified_at": now,
                }

        update_dict: dict = {
            "messages": [
                ToolMessage(
                    content=result,
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }

        if files_update:
            update_dict["files"] = files_update

        return Command(update=update_dict)

    return execute_python_code
