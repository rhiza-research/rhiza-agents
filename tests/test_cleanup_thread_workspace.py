"""Tests for cleanup_thread_workspace's two execution paths.

The helper either reuses the active per-thread sandbox (when one is
alive in _sandboxes) or spins up a temp sandbox just to do the rm.
The reused-sandbox path saves the ~5–15s sandbox-create cost on every
conversation delete where the sandbox was still alive when the delete
came in. Tests verify the dispatch.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from rhiza_agents.agents.tools import sandbox as sbx


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Each test runs against an empty _sandboxes dict and a known volume
    env var so the helper takes the path under test.
    """
    monkeypatch.setattr(sbx, "_sandboxes", {})
    monkeypatch.setenv("DAYTONA_HOMES_VOLUME", "homes")
    monkeypatch.delenv("DAYTONA_HOMES_MOUNT_PATH", raising=False)


class _RecordingSandbox:
    """Sandbox stub that records process.exec calls and the configured
    response (default: success)."""

    def __init__(self, exit_code: int = 0, result: str = ""):
        self.exec_calls: list[str] = []
        self._exit_code = exit_code
        self._result = result

        class _P:
            def exec(p_self, cmd, **_kwargs):  # noqa: N805
                self.exec_calls.append(cmd)
                return SimpleNamespace(exit_code=self._exit_code, result=self._result)

        self.process = _P()


def test_uses_active_sandbox_when_alive():
    """Path 1: the per-thread sandbox is in _sandboxes; cleanup runs the
    rm through it and never creates a temp sandbox."""
    active = _RecordingSandbox()
    sbx._sandboxes["thread-x"] = active

    with mock.patch.object(sbx, "_get_daytona") as mock_daytona:
        sbx.cleanup_thread_workspace("thread-x")

        # Temp-sandbox path was never invoked.
        mock_daytona.assert_not_called()

    # The rm command landed on the active sandbox.
    assert len(active.exec_calls) == 1
    assert "find /workspace -mindepth 1 -delete" in active.exec_calls[0]


def test_falls_back_to_temp_sandbox_when_none_alive():
    """Path 2: no per-thread sandbox in _sandboxes; cleanup creates a
    temp sandbox, runs the rm, and deletes the temp sandbox."""
    temp = _RecordingSandbox()

    fake_volume = SimpleNamespace(id="vol-id")
    fake_daytona = mock.MagicMock()
    fake_daytona.volume.get.return_value = fake_volume
    fake_daytona.create.return_value = temp

    with mock.patch.object(sbx, "_get_daytona", return_value=fake_daytona):
        sbx.cleanup_thread_workspace("thread-y")

    # Temp sandbox was created and deleted.
    fake_daytona.create.assert_called_once()
    fake_daytona.delete.assert_called_once_with(temp)
    # The rm ran on the temp sandbox.
    assert len(temp.exec_calls) == 1
    assert "find /workspace -mindepth 1 -delete" in temp.exec_calls[0]


def test_falls_back_to_temp_sandbox_when_active_path_raises():
    """Path 1 is attempted, raises, helper falls through to path 2.
    Verifies an unhealthy active sandbox doesn't strand the cleanup."""
    broken_active = _RecordingSandbox()

    def _broken_exec(cmd, **_kwargs):
        broken_active.exec_calls.append(cmd)
        raise RuntimeError("sandbox unreachable")

    broken_active.process.exec = _broken_exec
    sbx._sandboxes["thread-z"] = broken_active

    temp = _RecordingSandbox()
    fake_volume = SimpleNamespace(id="vol-id")
    fake_daytona = mock.MagicMock()
    fake_daytona.volume.get.return_value = fake_volume
    fake_daytona.create.return_value = temp

    with mock.patch.object(sbx, "_get_daytona", return_value=fake_daytona):
        sbx.cleanup_thread_workspace("thread-z")

    # Active path tried + failed.
    assert len(broken_active.exec_calls) == 1
    # Temp path ran the rm.
    fake_daytona.create.assert_called_once()
    fake_daytona.delete.assert_called_once_with(temp)
    assert len(temp.exec_calls) == 1


def test_no_op_when_homes_volume_unset(monkeypatch):
    """If DAYTONA_HOMES_VOLUME is empty there's no per-conversation
    workspace to clean — return immediately, don't touch any sandbox."""
    monkeypatch.setenv("DAYTONA_HOMES_VOLUME", "")
    active = _RecordingSandbox()
    sbx._sandboxes["thread-q"] = active

    with mock.patch.object(sbx, "_get_daytona") as mock_daytona:
        sbx.cleanup_thread_workspace("thread-q")

    # Nothing happened: no exec on the active sandbox, no Daytona client touch.
    assert active.exec_calls == []
    mock_daytona.assert_not_called()


def test_uses_custom_homes_mount_path(monkeypatch):
    """If DAYTONA_HOMES_MOUNT_PATH overrides /workspace, the rm targets
    that path — the helper does not hardcode SANDBOX_WORKSPACE."""
    monkeypatch.setenv("DAYTONA_HOMES_MOUNT_PATH", "/persist")
    active = _RecordingSandbox()
    sbx._sandboxes["thread-w"] = active

    with mock.patch.object(sbx, "_get_daytona"):
        sbx.cleanup_thread_workspace("thread-w")

    assert "find /persist -mindepth 1 -delete" in active.exec_calls[0]


@pytest.mark.parametrize(
    "bad_path",
    [
        "/",
        "/etc",
        "/var",
        "/home",
        "/root",
        "/usr",
        "/..",
        # Nested system paths: a child of a forbidden root is just as
        # dangerous to recursively empty as the root itself.
        "/etc/cron.d",
        "/var/lib/postgresql",
        "/usr/local/bin",
        "/home/someuser",
    ],
)
def test_refuses_unsafe_homes_mount_path(monkeypatch, bad_path):
    """A misconfigured homes mount path that resolves to a system tree
    (or a path nested under one) must not run the recursive delete — no
    exec, no temp sandbox."""
    monkeypatch.setenv("DAYTONA_HOMES_MOUNT_PATH", bad_path)
    active = _RecordingSandbox()
    sbx._sandboxes["thread-bad"] = active

    with mock.patch.object(sbx, "_get_daytona") as mock_daytona:
        sbx.cleanup_thread_workspace("thread-bad")

    # Guard fired before any filesystem-touching command ran.
    assert active.exec_calls == []
    mock_daytona.assert_not_called()


def test_is_safe_cleanup_path_rejects_nested_system_paths():
    """Prefix-based rejection: a path under any forbidden root is unsafe,
    while a legitimate deep mount outside every forbidden root is allowed."""
    # Nested under a forbidden system root → refused.
    assert sbx._is_safe_cleanup_path("/etc/cron.d") is False
    assert sbx._is_safe_cleanup_path("/var/lib/postgresql") is False
    assert sbx._is_safe_cleanup_path("/usr/local/lib") is False
    assert sbx._is_safe_cleanup_path("/home/alice/data") is False
    # A deep mount path outside every forbidden root → allowed.
    assert sbx._is_safe_cleanup_path("/mnt/homes") is True
    assert sbx._is_safe_cleanup_path("/mnt/homes/thread-123") is True
    # A prefix that merely starts with a forbidden root's name but is not
    # actually nested under it (no trailing slash boundary) is allowed.
    assert sbx._is_safe_cleanup_path("/etcdata") is True


def test_is_safe_cleanup_path_guard():
    """Direct check of the guard predicate: dedicated mounts allowed,
    system trees and empty/relative paths refused."""
    assert sbx._is_safe_cleanup_path("/workspace") is True
    assert sbx._is_safe_cleanup_path("/persist") is True
    assert sbx._is_safe_cleanup_path("/data/homes") is True
    assert sbx._is_safe_cleanup_path("/") is False
    assert sbx._is_safe_cleanup_path("") is False
    assert sbx._is_safe_cleanup_path("relative/path") is False
    assert sbx._is_safe_cleanup_path("/etc") is False
    # Normalization collapses traversal back to "/".
    assert sbx._is_safe_cleanup_path("/..") is False
