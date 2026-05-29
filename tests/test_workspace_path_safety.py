"""Tests for workspace_path's traversal guard.

workspace_path is the single choke point that maps a logical file path
(as supplied by the file-viewer endpoint, ultimately from a URL path
segment) to the absolute sandbox path that read_workspace_file /
write_workspace_file run as root. A logical path with ``..`` segments
would otherwise normalize to a root-owned file outside /workspace and
/data; the guard rejects it with ValueError so the route returns 404
without any root-level filesystem access.
"""

from types import SimpleNamespace

import pytest

from rhiza_agents.agents.tools.sandbox import (
    SANDBOX_DATA,
    SANDBOX_WORKSPACE,
    read_workspace_file,
    workspace_path,
    write_workspace_file,
)


def test_plain_workspace_path_maps_under_workspace():
    assert workspace_path("/foo.py") == f"{SANDBOX_WORKSPACE}/foo.py"


def test_nested_workspace_path():
    assert workspace_path("/sub/dir/bar.csv") == f"{SANDBOX_WORKSPACE}/sub/dir/bar.csv"


def test_data_path_kept_as_is():
    assert workspace_path("/data/forecast.parquet") == f"{SANDBOX_DATA}/forecast.parquet"


def test_data_root_kept_as_is():
    assert workspace_path("/data") == SANDBOX_DATA


@pytest.mark.parametrize(
    "logical",
    [
        "/../etc/passwd",
        "/../../etc/passwd",
        "/sub/../../etc/passwd",
        "/foo/../../bar",
    ],
)
def test_workspace_traversal_rejected(logical):
    # Escapes /workspace via .. segments — must raise, not resolve to a
    # root-owned file the read/write helpers would touch as root.
    with pytest.raises(ValueError):
        workspace_path(logical)


@pytest.mark.parametrize(
    "logical",
    [
        "/data/../../etc/x",
        "/data/../etc/passwd",
        "/data/sub/../../../etc/passwd",
    ],
)
def test_data_traversal_rejected(logical):
    # The /data prefix must be protected too: /data/../../etc/x escapes
    # the data root and must be rejected.
    with pytest.raises(ValueError):
        workspace_path(logical)


def test_dot_segments_within_workspace_allowed():
    # A .. that stays within /workspace normalizes cleanly and is fine.
    assert workspace_path("/sub/../foo.py") == f"{SANDBOX_WORKSPACE}/foo.py"


def test_dot_segments_within_data_allowed():
    assert workspace_path("/data/sub/../forecast.parquet") == f"{SANDBOX_DATA}/forecast.parquet"


# ---------------------------------------------------------------------------
# Cross-volume aliasing: a /data input must stay under /data, a workspace
# input must stay under /workspace. A path that lexically normalizes into
# the other root must be rejected, not silently re-rooted, so the
# state["files"] key namespace stays consistent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "logical",
    [
        "/data/../workspace/x",
        "/data/../../workspace/x",
        "/data/sub/../../workspace/y",
    ],
)
def test_data_input_resolving_into_workspace_rejected(logical):
    with pytest.raises(ValueError):
        workspace_path(logical)


@pytest.mark.parametrize(
    "logical",
    [
        "/../data/x",
        "/sub/../../data/y",
    ],
)
def test_workspace_input_resolving_into_data_rejected(logical):
    with pytest.raises(ValueError):
        workspace_path(logical)


# ---------------------------------------------------------------------------
# Symlink-escape guard (in-sandbox realpath check).
#
# workspace_path's normpath is lexical; it cannot see a symlink the agent
# planted in the sandbox FS via HITL-approved execute_python_code. The
# read/write helpers resolve the real path in-sandbox via ``realpath -m``
# and reject anything that resolves outside /workspace or /data, before
# any read or write runs.
# ---------------------------------------------------------------------------


class _RealpathSandbox:
    """Sandbox stub: answers ``realpath -m`` with a fixed escaped target,
    and records every other exec so we can assert no read/write fired."""

    def __init__(self, realpath_result: str, realpath_exit: int = 0):
        self._realpath_result = realpath_result
        self._realpath_exit = realpath_exit
        self.exec_calls: list[str] = []

        class _P:
            def exec(p_self, cmd, **_kwargs):  # noqa: N805
                self.exec_calls.append(cmd)
                if cmd.startswith("realpath"):
                    return SimpleNamespace(exit_code=self._realpath_exit, result=self._realpath_result)
                # Any non-realpath exec (the actual read/write) — should
                # not be reached when the guard rejects.
                return SimpleNamespace(exit_code=0, result="")

        self.process = _P()


def test_read_rejects_symlink_escape_no_read_exec():
    # realpath resolves the file to a target outside the roots → reject
    # before the base64 read exec runs.
    sandbox = _RealpathSandbox(realpath_result="/etc/passwd")
    with pytest.raises(ValueError):
        read_workspace_file(sandbox, f"{SANDBOX_WORKSPACE}/evil_link")
    # Only the realpath probe ran; the read (test -f / base64) never did.
    assert len(sandbox.exec_calls) == 1
    assert sandbox.exec_calls[0].startswith("realpath")


def test_write_rejects_symlink_escape_no_write_exec():
    # realpath of the parent dir resolves outside the roots → reject
    # before mkdir/base64 write runs.
    sandbox = _RealpathSandbox(realpath_result="/etc")
    with pytest.raises(ValueError):
        write_workspace_file(sandbox, f"{SANDBOX_WORKSPACE}/sub/evil", b"data")
    assert len(sandbox.exec_calls) == 1
    assert sandbox.exec_calls[0].startswith("realpath")


def test_read_allows_contained_realpath():
    # A realpath that stays under /workspace passes the guard; the read
    # exec then runs (returns empty here → FileNotFoundError path).
    sandbox = _RealpathSandbox(realpath_result=f"{SANDBOX_WORKSPACE}/real.py")

    class _OkExec:
        def exec(self, cmd, **_kwargs):
            sandbox.exec_calls.append(cmd)
            if cmd.startswith("realpath"):
                return SimpleNamespace(exit_code=0, result=f"{SANDBOX_WORKSPACE}/real.py")
            # base64 read returns content.
            import base64 as _b64

            return SimpleNamespace(exit_code=0, result=_b64.b64encode(b"hello").decode())

    sandbox.process = _OkExec()
    assert read_workspace_file(sandbox, f"{SANDBOX_WORKSPACE}/real.py") == b"hello"
