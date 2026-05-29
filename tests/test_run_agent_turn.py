"""Unit tests for the shared turn driver ``run_agent_turn``.

The driver owns the astream loop, auto-resume re-loop, tool-event dedup, and
interrupt handling. These tests drive a fake graph whose ``astream`` yields the
same ``version="v2"`` dict chunks LangGraph produces ({"type","data","ns"}),
and monkeypatch ``make_langfuse_handler`` to None so no real callbacks are
constructed.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from rhiza_agents.agents.turn import MAX_AUTO_RESUMES, run_agent_turn


@pytest.fixture(autouse=True)
def _no_langfuse(monkeypatch):
    # No trace_id events, no callbacks — keeps the driver transport-free.
    monkeypatch.setattr("rhiza_agents.agents.turn.make_langfuse_handler", lambda **k: None)


class _Token:
    """A minimal AI-message token exposing ``content`` (a plain text string)."""

    def __init__(self, text):
        self.content = text


def _tool_message(name, content, tool_call_id="tc"):
    """A real langchain ToolMessage so the driver's isinstance guards fire."""
    return ToolMessage(name=name, content=content, tool_call_id=tool_call_id)


def _ai_tool_call_message(tool_calls):
    """A real langchain AIMessage carrying tool_calls."""
    return AIMessage(content="", tool_calls=tool_calls)


class _FakeGraph:
    """``astream`` yields the chunks for the current round; each call advances
    to the next round so a Command(resume=...) input drives the next round."""

    def __init__(self, rounds):
        self._rounds = rounds
        self.astream_calls = 0
        self.inputs = []

    def astream(self, stream_input, config=None, **kwargs):
        self.inputs.append(stream_input)
        idx = min(self.astream_calls, len(self._rounds) - 1)
        self.astream_calls += 1
        return self._emit(self._rounds[idx])

    async def _emit(self, chunks):
        for c in chunks:
            yield c


def _msg_chunk(text, node="assistant"):
    return {"type": "messages", "data": (_Token(text), {"langgraph_node": node}), "ns": ()}


class _IntrValue:
    """An interrupt wrapper exposing ``.value`` like a langgraph Interrupt."""

    def __init__(self, value):
        self.value = value


def _interrupt_chunk(value, ns=()):
    return {"type": "updates", "data": {"__interrupt__": [_IntrValue(value)]}, "ns": ns}


def _tool_calls_chunk(tool_calls, node="assistant"):
    return {
        "type": "updates",
        "data": {node: {"messages": [_ai_tool_call_message(tool_calls)]}},
        "ns": (),
    }


def _tool_result_chunk(name, content, tool_call_id="tc", node="tools"):
    return {
        "type": "updates",
        "data": {node: {"messages": [_tool_message(name, content, tool_call_id)]}},
        "ns": (),
    }


async def _collect(gen):
    return [ev async for ev in gen]


# --- (a) auto mode auto-resumes on an interrupt then completes ---


@pytest.mark.asyncio
async def test_auto_mode_resumes_then_completes():
    graph = _FakeGraph(
        rounds=[
            [_msg_chunk("Working"), _interrupt_chunk({"action_requests": []})],
            [_msg_chunk("Done.")],
        ]
    )
    events = await _collect(
        run_agent_turn(
            graph,
            thread_id="t1",
            stream_input={"messages": ["hi"]},
            execution_mode="auto",
        )
    )

    assert graph.astream_calls == 2  # auto-resume happened
    # Second round was driven by a resume Command, not the original input.
    assert isinstance(graph.inputs[1], Command)
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens == ["Working", "Done."]
    # Interrupt is auto-approved, never surfaced.
    assert all(e["type"] != "interrupt" for e in events)


# --- (b) review mode yields the interrupt and stops (no auto-resume) ---


@pytest.mark.asyncio
async def test_review_mode_yields_interrupt_and_stops():
    graph = _FakeGraph(
        rounds=[
            [_msg_chunk("Need approval "), _interrupt_chunk({"action_requests": [{"name": "run_file"}]})],
            [_msg_chunk("should not be reached")],
        ]
    )
    events = await _collect(
        run_agent_turn(
            graph,
            thread_id="t1",
            stream_input={"messages": ["go"]},
            execution_mode="review",
        )
    )

    assert graph.astream_calls == 1  # no resume in review mode
    interrupts = [e for e in events if e["type"] == "interrupt"]
    assert len(interrupts) == 1
    # The raw interrupt value is yielded, unenriched.
    assert interrupts[0]["value"] == {"action_requests": [{"name": "run_file"}]}


@pytest.mark.asyncio
async def test_subgraph_interrupt_with_ns_is_skipped():
    # An interrupt from a subgraph (non-empty ns) must not surface — only the
    # top-level (empty ns) duplicate does.
    graph = _FakeGraph(rounds=[[_interrupt_chunk({"x": 1}, ns=("sub",))]])
    events = await _collect(
        run_agent_turn(graph, thread_id="t1", stream_input={"messages": ["go"]}, execution_mode="review")
    )
    assert all(e["type"] != "interrupt" for e in events)


# --- (c) tool-event dedup across duplicate chunks ---


@pytest.mark.asyncio
async def test_tool_event_dedup_across_duplicate_chunks():
    # subgraphs=True surfaces the same tool call/result from both the subgraph
    # and the parent. The driver dedups by id within a turn.
    tc = {"id": "call-1", "name": "run_file", "args": {"path": "x.py"}}
    graph = _FakeGraph(
        rounds=[
            [
                _tool_calls_chunk([tc]),
                _tool_calls_chunk([tc]),  # duplicate from parent
                _tool_result_chunk("run_file", "ok", tool_call_id="call-1"),
                _tool_result_chunk("run_file", "ok", tool_call_id="call-1"),  # duplicate
            ]
        ]
    )
    events = await _collect(
        run_agent_turn(graph, thread_id="t1", stream_input={"messages": ["go"]}, execution_mode="auto")
    )

    starts = [e for e in events if e["type"] == "tool_start"]
    ends = [e for e in events if e["type"] == "tool_end"]
    assert len(starts) == 1
    assert starts[0] == {"type": "tool_start", "name": "run_file", "args": {"path": "x.py"}}
    assert len(ends) == 1
    assert ends[0]["name"] == "run_file"
    # run_file result emits a files_changed event.
    assert any(e["type"] == "files_changed" for e in events)


@pytest.mark.asyncio
async def test_dedup_persists_across_resume_rounds():
    # A tool call seen in round one must not re-emit when the same chunk recurs
    # after an auto-resume.
    tc = {"id": "call-9", "name": "run_file", "args": {}}
    graph = _FakeGraph(
        rounds=[
            [_tool_calls_chunk([tc]), _interrupt_chunk({})],
            [_tool_calls_chunk([tc])],  # same id again after resume
        ]
    )
    events = await _collect(
        run_agent_turn(graph, thread_id="t1", stream_input={"messages": ["go"]}, execution_mode="auto")
    )
    starts = [e for e in events if e["type"] == "tool_start"]
    assert len(starts) == 1


# --- (d) resume cap ---


@pytest.mark.asyncio
async def test_resume_cap_bounds_the_loop():
    # A graph that interrupts every round must stop after max_resumes + 1 calls.
    graph = _FakeGraph(rounds=[[_interrupt_chunk({})]])
    await _collect(
        run_agent_turn(
            graph,
            thread_id="t1",
            stream_input={"messages": ["loop"]},
            execution_mode="auto",
            max_resumes=3,
        )
    )
    assert graph.astream_calls == 4  # max_resumes + 1


@pytest.mark.asyncio
async def test_default_resume_cap():
    graph = _FakeGraph(rounds=[[_interrupt_chunk({})]])
    await _collect(run_agent_turn(graph, thread_id="t1", stream_input={"messages": ["loop"]}, execution_mode="auto"))
    assert graph.astream_calls == MAX_AUTO_RESUMES + 1


# --- trace_id emission when a handler is present ---


@pytest.mark.asyncio
async def test_trace_id_emitted_when_handler_present(monkeypatch):
    monkeypatch.setattr("rhiza_agents.agents.turn.make_langfuse_handler", lambda **k: object())
    captured_config = {}

    class _G(_FakeGraph):
        def astream(self, stream_input, config=None, **kwargs):
            captured_config.update(config or {})
            return super().astream(stream_input, config=config, **kwargs)

    graph = _G(rounds=[[_msg_chunk("hi")]])
    events = await _collect(
        run_agent_turn(
            graph,
            thread_id="t1",
            stream_input={"messages": ["go"]},
            execution_mode="auto",
            user_id="u1",
        )
    )
    trace_events = [e for e in events if e["type"] == "trace_id"]
    assert len(trace_events) == 1
    assert isinstance(trace_events[0]["trace_id"], str)
    # The handler is wired into the config callbacks and metadata carries the
    # session id + user id the web path expects.
    assert "callbacks" in captured_config
    assert captured_config["metadata"]["langfuse_session_id"] == "t1"
    assert captured_config["metadata"]["langfuse_user_id"] == "u1"


# --- ToolMessage tokens and the "tools" node are not streamed as content ---


@pytest.mark.asyncio
async def test_tool_node_messages_not_streamed_as_tokens():
    graph = _FakeGraph(
        rounds=[
            [
                {
                    "type": "messages",
                    "data": (_tool_message("run_file", "out"), {"langgraph_node": "assistant"}),
                    "ns": (),
                },
                _msg_chunk("real", node="tools"),  # tools node output is skipped
                _msg_chunk("kept", node="assistant"),
            ]
        ]
    )
    events = await _collect(
        run_agent_turn(graph, thread_id="t1", stream_input={"messages": ["go"]}, execution_mode="auto")
    )
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens == ["kept"]
