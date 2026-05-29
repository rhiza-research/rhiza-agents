"""Tests for the file-size cap in fetch_file_content.

The file endpoint reads the whole file into memory; without a cap a
multi-GB artifact on /data would OOM the server. fetch_file_content
stats the file first and rejects anything above MAX_FETCH_FILE_BYTES
with FileTooLargeError BEFORE read_workspace_file pulls the content in.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from rhiza_agents.agents.tools import files as files_mod
from rhiza_agents.agents.tools.files import (
    MAX_FETCH_FILE_BYTES,
    FileTooLargeError,
    fetch_file_content,
)


class _StatSandbox:
    """Sandbox stub whose process.exec answers the stat with a fixed size."""

    def __init__(self, size: int, mtime: int = 1700000000):
        self._size = size
        self._mtime = mtime
        self.read_called = False

        class _P:
            def exec(p_self, cmd, **_kwargs):  # noqa: N805
                # Only the stat command is exercised in these tests.
                return SimpleNamespace(exit_code=0, result=f"{self._size}|{self._mtime}")

        self.process = _P()


@pytest.mark.asyncio
async def test_oversized_file_rejected_before_read(monkeypatch):
    sandbox = _StatSandbox(size=MAX_FETCH_FILE_BYTES + 1)

    def _fake_read(_sandbox, _abs_path):
        # If this fires the cap failed to short-circuit the read.
        raise AssertionError("read_workspace_file must not run for oversized file")

    monkeypatch.setattr(files_mod, "read_workspace_file", _fake_read)
    monkeypatch.setattr(
        "rhiza_agents.agents.tools.sandbox._get_or_create_sandbox",
        lambda _tid: sandbox,
    )

    with pytest.raises(FileTooLargeError) as exc:
        await fetch_file_content("thread-1", "/big.bin")
    assert exc.value.size == MAX_FETCH_FILE_BYTES + 1


@pytest.mark.asyncio
async def test_at_limit_file_is_read(monkeypatch):
    sandbox = _StatSandbox(size=MAX_FETCH_FILE_BYTES)

    monkeypatch.setattr(files_mod, "read_workspace_file", lambda _s, _p: b"x" * 10)
    monkeypatch.setattr(
        "rhiza_agents.agents.tools.sandbox._get_or_create_sandbox",
        lambda _tid: sandbox,
    )

    content, _modified = await fetch_file_content("thread-1", "/atlimit.bin")
    assert content == b"x" * 10


@pytest.mark.asyncio
async def test_oversized_legacy_fallback_rejected_before_write(monkeypatch):
    # File absent on the volume (stat fails), and the legacy fallback
    # itself is over the cap — must reject without writing it.
    class _MissingSandbox:
        def __init__(self):
            class _P:
                def exec(p_self, cmd, **_kwargs):  # noqa: N805
                    return SimpleNamespace(exit_code=1, result="")

            self.process = _P()

    sandbox = _MissingSandbox()

    def _fake_write(*_args, **_kwargs):
        raise AssertionError("write_workspace_file must not run for oversized fallback")

    monkeypatch.setattr(
        "rhiza_agents.agents.tools.sandbox._get_or_create_sandbox",
        lambda _tid: sandbox,
    )
    monkeypatch.setattr(
        "rhiza_agents.agents.tools.sandbox.write_workspace_file",
        _fake_write,
    )

    oversized = b"x" * (MAX_FETCH_FILE_BYTES + 1)
    with pytest.raises(FileTooLargeError):
        await fetch_file_content("thread-1", "/big.bin", legacy_fallback=oversized)


@pytest.mark.asyncio
async def test_route_maps_file_too_large_to_413(monkeypatch):
    from fastapi import HTTPException

    from rhiza_agents.routes import conversations as conv

    db = mock.MagicMock()
    db.get_conversation = mock.AsyncMock(return_value={"user_id": "owner-1"})

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=db,
                checkpointer=object(),
                mcp_tools=[],
                mcp_tools_by_server={},
                vectorstore_manager=object(),
                config=object(),
            )
        ),
        session={"user": {"sub": "owner-1"}},
    )

    monkeypatch.setattr(conv, "_get_mcp_tools_for_owner", mock.AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(conv, "_get_skill_tools_for_owner", mock.AsyncMock(return_value={}))
    fake_graph = mock.MagicMock()
    fake_graph.aget_state = mock.AsyncMock(return_value=SimpleNamespace(values={"files": {}}))
    monkeypatch.setattr(conv, "get_agent_graph", mock.AsyncMock(return_value=fake_graph))

    async def _fake_fetch(_tid, lookup_path, legacy_fallback=None):
        raise FileTooLargeError(lookup_path, MAX_FETCH_FILE_BYTES + 1)

    monkeypatch.setattr(conv, "fetch_file_content", _fake_fetch)

    with pytest.raises(HTTPException) as exc:
        await conv.get_conversation_file(request, "conv-1", "big.bin", user={})
    assert exc.value.status_code == 413
