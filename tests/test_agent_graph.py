"""Tests for the agent graph build and config merge."""

import pytest

from rhiza_agents.agents.registry import AGENT_ID, get_default_configs, merge_configs
from rhiza_agents.db.models import AgentConfig


class _Tool:
    def __init__(self, name):
        self.name = name


def test_merge_configs_ignores_unknown_override_ids():
    # Stale per-worker rows (data_analyst/code_runner/...) left in
    # user_agent_configs from the pre-collapse topology must not resurrect.
    defaults = get_default_configs()
    overrides = [
        {"id": "data_analyst", "name": "Stale", "type": "worker", "system_prompt": "p", "tools": ["mcp:foo"]},
        {"id": AGENT_ID, "name": "Renamed", "type": "worker", "system_prompt": "p", "tools": []},
    ]
    merged = merge_configs(defaults, overrides)
    assert {c.id for c in merged} == {AGENT_ID}
    assert next(c for c in merged if c.id == AGENT_ID).name == "Renamed"


@pytest.mark.asyncio
async def test_build_agent_graph_raises_when_no_enabled_config(monkeypatch):
    from rhiza_agents.agents import graph as g

    async def fake_resolve(*a, **k):
        return [_Tool("run_file")]

    monkeypatch.setattr(g, "_resolve_tools", fake_resolve)
    monkeypatch.setattr(g, "_build_worker_middleware", lambda tools: [])
    monkeypatch.setattr(g, "create_agent", lambda *a, **k: "GRAPH")
    monkeypatch.setattr(g, "ChatAnthropic", lambda **k: object())

    configs = [
        AgentConfig(id=AGENT_ID, name="A", type="worker", system_prompt="p", tools=[], enabled=False),
    ]
    with pytest.raises(ValueError, match="No enabled agent"):
        await g.build_agent_graph(configs, [], checkpointer=None)
