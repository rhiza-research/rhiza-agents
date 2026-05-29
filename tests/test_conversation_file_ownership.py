"""Tests for ownership-gated legacy migration in get_conversation_file.

The file-content endpoint serves both the conversation owner and
read-only viewers (shared-link access). Only the owner may trigger the
lazy migration write that materializes pre-volume state-stored content
onto the owner's workspace volume. A non-owner viewer requesting a file
that isn't on the volume yet must get a read-only 404 with no write to
the owner's volume — i.e. fetch_file_content must be called with
legacy_fallback=None.

Ownership is decided by db.get_conversation(conversation_id, user_id):
truthy == requester is the owner; the get_conversation_by_id fallback ==
read-only viewer.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from rhiza_agents.routes import conversations as conv


class _FakeRequest:
    """Minimal Request stand-in exposing app.state and session.

    The route handler reads its dependencies (db, checkpointer,
    mcp_tools, vectorstore_manager, user_id) off request.app.state and
    request.session.
    """

    def __init__(self, db, user_sub):
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                db=db,
                checkpointer=object(),
                mcp_tools=[],
                mcp_tools_by_server={},
                vectorstore_manager=object(),
                config=object(),
            )
        )
        self.session = {"user": {"sub": user_sub}}


def _make_db(*, owner_lookup, by_id_lookup):
    """Build an async-mocked db with the two ownership lookups stubbed."""
    db = mock.MagicMock()
    db.get_conversation = mock.AsyncMock(return_value=owner_lookup)
    db.get_conversation_by_id = mock.AsyncMock(return_value=by_id_lookup)
    return db


@pytest.mark.asyncio
async def test_non_owner_gets_no_legacy_fallback(monkeypatch):
    """A viewer (get_conversation returns None, get_conversation_by_id
    returns the row) must not trigger a migration write: the handler
    passes legacy_fallback=None and the missing file 404s."""
    db = _make_db(owner_lookup=None, by_id_lookup={"user_id": "owner-1"})
    request = _FakeRequest(db, user_sub="viewer-2")

    captured = {}

    async def _fake_fetch(thread_id, lookup_path, legacy_fallback=None):
        captured["legacy_fallback"] = legacy_fallback
        raise FileNotFoundError(lookup_path)

    monkeypatch.setattr(conv, "fetch_file_content", _fake_fetch)
    # Guard: the owner-only state lookup must not run for a viewer.
    monkeypatch.setattr(
        conv,
        "_get_mcp_tools_for_owner",
        mock.AsyncMock(side_effect=AssertionError("viewer must not read owner state")),
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await conv.get_conversation_file(request, "conv-1", "secret.txt", user={})

    assert exc.value.status_code == 404
    assert captured["legacy_fallback"] is None


@pytest.mark.asyncio
async def test_owner_gets_legacy_fallback_from_state(monkeypatch):
    """The owner (get_conversation returns the row) gets the state's
    stored content passed as legacy_fallback so fetch_file_content can
    migrate it onto the volume."""
    db = _make_db(owner_lookup={"user_id": "owner-1"}, by_id_lookup=None)
    request = _FakeRequest(db, user_sub="owner-1")

    # State carries legacy content for the requested path.
    fake_state = SimpleNamespace(values={"files": {"/secret.txt": {"content": ["hello world"], "encoding": "utf-8"}}})
    fake_graph = mock.MagicMock()
    fake_graph.aget_state = mock.AsyncMock(return_value=fake_state)

    monkeypatch.setattr(conv, "_get_mcp_tools_for_owner", mock.AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(conv, "_get_skill_tools_for_owner", mock.AsyncMock(return_value={}))
    monkeypatch.setattr(conv, "get_agent_graph", mock.AsyncMock(return_value=fake_graph))

    captured = {}

    async def _fake_fetch(thread_id, lookup_path, legacy_fallback=None):
        captured["legacy_fallback"] = legacy_fallback
        return (b"hello world", "2026-01-01T00:00:00+00:00")

    monkeypatch.setattr(conv, "fetch_file_content", _fake_fetch)

    result = await conv.get_conversation_file(request, "conv-1", "secret.txt", user={})

    assert captured["legacy_fallback"] == b"hello world"
    assert result["content"] == "hello world"
    assert result["encoding"] == "utf-8"
