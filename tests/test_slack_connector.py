"""Unit tests for the Slack connector: config parsing, idle window, image
detection, the skills-only tool policy, and event routing."""

import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from rhiza_agents.config import _parse_channel_user_map
from rhiza_agents.db.models import AgentConfig
from rhiza_agents.slack_connector import (
    MAX_AUTO_RESUMES,
    THREAD_IDLE_SECONDS,
    SlackConnector,
    _conversation_is_active,
)

# --- config: channel -> user map parsing ---


def test_parse_channel_user_map_valid():
    assert _parse_channel_user_map('{"C1": "u1", "C2": "u2"}') == {"C1": "u1", "C2": "u2"}


def test_parse_channel_user_map_empty():
    assert _parse_channel_user_map("") == {}
    assert _parse_channel_user_map("   ") == {}


def test_parse_channel_user_map_invalid_json():
    assert _parse_channel_user_map("not json") == {}


def test_parse_channel_user_map_non_dict():
    assert _parse_channel_user_map('["a", "b"]') == {}


def test_parse_channel_user_map_coerces_to_str():
    assert _parse_channel_user_map('{"C1": 123}') == {"C1": "123"}


# --- idle window + image detection helpers ---


def test_conversation_active_recent():
    assert _conversation_is_active({"updated_at": datetime.now(UTC)}) is True


def test_conversation_expired():
    old = datetime.now(UTC) - timedelta(seconds=THREAD_IDLE_SECONDS + 60)
    assert _conversation_is_active({"updated_at": old}) is False


def test_conversation_none_updated_at_active():
    assert _conversation_is_active({"updated_at": None}) is True


def test_conversation_naive_string_recent():
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert _conversation_is_active({"updated_at": naive}) is True


def test_conversation_unparseable_fails_closed():
    # On the auto-approve path a corrupt timestamp must not keep a thread alive.
    assert _conversation_is_active({"updated_at": "not-a-date"}) is False


def test_new_image_paths_new_overwrite_excludes_data():
    before = {"/workspace/a.png": {"m": 1}, "/data/cache.png": {"m": 1}}
    after = {
        "/workspace/a.png": {"m": 2},  # overwritten -> included
        "/workspace/b.png": {"m": 1},  # new -> included
        "/data/cache.png": {"m": 2},  # shared cache -> excluded
        "/workspace/c.txt": {"m": 1},  # non-image -> excluded
    }
    assert SlackConnector._new_image_paths(before, after) == {"/workspace/a.png", "/workspace/b.png"}


# --- agent tool policy (skills-only: run_file is the only execution tool) ---


class _Tool:
    def __init__(self, name):
        self.name = name


@pytest.mark.asyncio
async def test_resolve_tools_yields_run_file_not_execute_python_code(monkeypatch):
    """The real _resolve_tools for a sandbox:daytona config yields run_file and
    never an execute_python_code tool — the tool no longer exists."""
    from rhiza_agents.agents import graph as g
    from rhiza_agents.agents.tools import sandbox

    # The removed factory must be gone entirely.
    assert not hasattr(sandbox, "make_execute_python_code")

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(
        "rhiza_agents.agents.tools.files.make_run_file",
        lambda db=None: _Tool("run_file"),
    )

    config = AgentConfig(id="assistant", name="W", type="worker", system_prompt="w", tools=["sandbox:daytona"])
    tools = await g._resolve_tools(config, [])
    names = {t.name for t in tools}
    assert "run_file" in names
    assert "execute_python_code" not in names


@pytest.mark.asyncio
async def test_single_agent_dedups_tools_across_workers(monkeypatch):
    from rhiza_agents.agents import graph as g

    async def fake_resolve(config, *a, **k):
        return [_Tool("query_forecast"), _Tool("run_file")]

    captured = {}

    monkeypatch.setattr(g, "_resolve_tools", fake_resolve)
    monkeypatch.setattr(g, "_build_worker_middleware", lambda tools: [])
    monkeypatch.setattr(g, "create_agent", lambda model, tools, **k: captured.update(tools=tools))
    monkeypatch.setattr(g, "ChatAnthropic", lambda **k: object())

    configs = [
        AgentConfig(id="assistant", name="W1", type="worker", system_prompt="w", tools=["mcp:sheerwater"]),
        AgentConfig(id="w2", name="W2", type="worker", system_prompt="w", tools=["mcp:sheerwater"]),
    ]
    await g.build_agent_graph(configs, [], checkpointer=None)

    names = [t.name for t in captured["tools"]]
    assert names.count("query_forecast") == 1
    assert names.count("run_file") == 1


# --- event routing ---


class _FakeApp:
    def __init__(self, *a, **k):
        self.client = AsyncMock()
        self._handlers = {}

    def event(self, name):
        def deco(fn):
            self._handlers[name] = fn
            return fn

        return deco


class _FakeHandler:
    def __init__(self, *a, **k):
        pass


def _make_connector(monkeypatch, channel_map, conversations):
    import slack_bolt.adapter.socket_mode.async_handler as handler_mod
    import slack_bolt.app.async_app as async_app_mod

    monkeypatch.setattr(async_app_mod, "AsyncApp", _FakeApp)
    monkeypatch.setattr(handler_mod, "AsyncSocketModeHandler", _FakeHandler)

    config = types.SimpleNamespace(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_channel_user_map=channel_map,
    )
    db = types.SimpleNamespace(
        get_conversation_by_id=AsyncMock(side_effect=lambda cid: conversations.get(cid)),
        create_conversation=AsyncMock(),
        touch_conversation=AsyncMock(),
    )
    conn = SlackConnector(
        config=config,
        db=db,
        checkpointer=None,
        system_mcp_tools=[],
        system_mcp_tools_by_server={},
    )
    conn.bot_user_id = "UBOT"
    conn._run_turn = AsyncMock()
    return conn, db


@pytest.mark.asyncio
async def test_message_unmapped_channel_ignored(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {}, {})
    await conn._on_message({"channel": "Cx", "thread_ts": "1.1", "text": "hi"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_from_bot_ignored(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {})
    await conn._on_message({"channel": "C1", "thread_ts": "1.1", "bot_id": "B1", "text": "hi"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_top_level_no_thread_ignored(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {})
    await conn._on_message({"channel": "C1", "text": "hi"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_mentioning_bot_deferred_to_mention_handler(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {"1.1": {"updated_at": datetime.now(UTC)}})
    await conn._on_message({"channel": "C1", "thread_ts": "1.1", "text": "<@UBOT> hi"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_owned_active_thread_runs(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {"1.1": {"updated_at": datetime.now(UTC)}})
    await conn._on_message({"channel": "C1", "thread_ts": "1.1", "text": "continue"}, AsyncMock())
    conn._run_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_expired_thread_ignored(monkeypatch):
    old = datetime.now(UTC) - timedelta(seconds=THREAD_IDLE_SECONDS + 60)
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {"1.1": {"updated_at": old}})
    await conn._on_message({"channel": "C1", "thread_ts": "1.1", "text": "hello"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_unknown_thread_ignored(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {})
    await conn._on_message({"channel": "C1", "thread_ts": "1.1", "text": "hello"}, AsyncMock())
    conn._run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_mention_new_thread_creates_and_runs(monkeypatch):
    conn, db = _make_connector(monkeypatch, {"C1": "u1"}, {})
    await conn._on_app_mention({"channel": "C1", "ts": "9.9", "text": "<@UBOT> plot it"}, AsyncMock())
    db.create_conversation.assert_awaited_once()
    args, _ = db.create_conversation.await_args
    assert args[0] == "9.9"  # conversation keyed to the message ts
    conn._run_turn.assert_awaited_once()
    _, kwargs = conn._run_turn.await_args
    assert kwargs["thread_root"] == "9.9"
    assert kwargs["text"] == "plot it"  # mention stripped


@pytest.mark.asyncio
async def test_mention_unmapped_channel_ignored(monkeypatch):
    conn, db = _make_connector(monkeypatch, {}, {})
    await conn._on_app_mention({"channel": "Cx", "ts": "9.9", "text": "<@UBOT> hi"}, AsyncMock())
    conn._run_turn.assert_not_awaited()
    db.create_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_mention_existing_thread_resumes_without_create(monkeypatch):
    conn, db = _make_connector(monkeypatch, {"C1": "u1"}, {"5.5": {"updated_at": datetime.now(UTC)}})
    await conn._on_app_mention({"channel": "C1", "ts": "9.9", "thread_ts": "5.5", "text": "<@UBOT> more"}, AsyncMock())
    db.create_conversation.assert_not_awaited()
    _, kwargs = conn._run_turn.await_args
    assert kwargs["thread_root"] == "5.5"  # keyed to the thread root, not the new ts


@pytest.mark.asyncio
async def test_mention_ignores_bot_and_subtype(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {})
    await conn._on_app_mention({"channel": "C1", "ts": "9.9", "bot_id": "B1", "text": "<@UBOT> hi"}, AsyncMock())
    await conn._on_app_mention(
        {"channel": "C1", "ts": "9.9", "subtype": "message_changed", "text": "<@UBOT> hi"}, AsyncMock()
    )
    conn._run_turn.assert_not_awaited()


def test_parse_channel_user_map_skips_non_scalar():
    from rhiza_agents.config import _parse_channel_user_map as parse

    assert parse('{"C1": {"x": 1}, "C2": "u2", "C3": [1]}') == {"C2": "u2"}


# --- _run_turn stream loop (the path C1 broke: version="v2" dict chunks, auto-resume, image upload) ---


class _FakeState:
    def __init__(self, files):
        self.values = {"files": files}


class _FakeGraph:
    """astream yields version='v2' dict chunks; aget_state returns the
    before/after file snapshots in order."""

    def __init__(self, rounds, snapshots):
        self._rounds = rounds
        self._astream_calls = 0
        self.aget_state = AsyncMock(side_effect=snapshots)

    @property
    def astream_calls(self):
        return self._astream_calls

    def astream(self, _input, config=None, **kwargs):
        idx = min(self._astream_calls, len(self._rounds) - 1)
        self._astream_calls += 1
        return self._emit(self._rounds[idx])

    async def _emit(self, chunks):
        for c in chunks:
            yield c


def _msg_chunk(text):
    return {"type": "messages", "data": (text, {"langgraph_node": "assistant"}), "ns": ()}


_INTERRUPT_CHUNK = {"type": "updates", "data": {"__interrupt__": [object()]}, "ns": ()}


@pytest.mark.asyncio
async def test_run_turn_streams_resumes_and_uploads_image(monkeypatch):
    conn, db = _make_connector(monkeypatch, {"C1": "u1"}, {})
    del conn._run_turn  # restore the real method (routing tests mock it)
    graph = _FakeGraph(
        rounds=[[_msg_chunk("Working "), _INTERRUPT_CHUNK], [_msg_chunk("Done: plot ready.")]],
        snapshots=[_FakeState({}), _FakeState({"/workspace/plot.png": {}})],
    )
    monkeypatch.setattr(conn, "_build_graph", AsyncMock(return_value=(graph, {}, {})))
    monkeypatch.setattr("rhiza_agents.agents.turn.extract_content_blocks_from_token", lambda t: (t, ""))
    monkeypatch.setattr("rhiza_agents.agents.turn.make_langfuse_handler", lambda **k: None)
    monkeypatch.setattr("rhiza_agents.slack_connector.fetch_file_content", AsyncMock(return_value=(b"PNGBYTES", "iso")))
    client = AsyncMock()

    await conn._run_turn(channel="C1", thread_root="9.9", user_id="u1", text="plot it", client=client)

    assert graph.astream_calls == 2  # auto-resume happened
    client.chat_postMessage.assert_awaited_once()
    _, kw = client.chat_postMessage.await_args
    assert kw["thread_ts"] == "9.9"
    assert kw["text"] == "Working Done: plot ready."
    client.files_upload_v2.assert_awaited_once()
    _, ukw = client.files_upload_v2.await_args
    assert ukw["thread_ts"] == "9.9"
    assert ukw["file"] == b"PNGBYTES"
    assert ukw["filename"] == "plot.png"
    db.touch_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_turn_caps_auto_resume(monkeypatch):
    conn, _ = _make_connector(monkeypatch, {"C1": "u1"}, {})
    del conn._run_turn
    graph = _FakeGraph(rounds=[[_INTERRUPT_CHUNK]], snapshots=[_FakeState({}), _FakeState({})])
    monkeypatch.setattr(conn, "_build_graph", AsyncMock(return_value=(graph, {}, {})))
    monkeypatch.setattr("rhiza_agents.agents.turn.extract_content_blocks_from_token", lambda t: (t, ""))
    monkeypatch.setattr("rhiza_agents.agents.turn.make_langfuse_handler", lambda **k: None)
    client = AsyncMock()

    await conn._run_turn(channel="C1", thread_root="9.9", user_id="u1", text="loop", client=client)

    assert graph.astream_calls == MAX_AUTO_RESUMES + 1  # bounded, no infinite loop
