"""Tests for the interrupt-payload enrichment that drives the
missing-credential warnings in the approval card.

The helper is a pure async function that takes an interrupt payload
(``{"action_requests": [...]}``) plus ``(db, user_id)`` and returns a
payload with ``missing_credentials`` annotated per action_request and as
a top-level aggregate. Small surface area; easy to unit-test without
bringing up LangGraph or the SSE stream.
"""

import asyncio

from rhiza_agents.routes.chat import _enrich_interrupt_payload


class _StubDB:
    """Minimal DB stub exposing only the one method the helper uses."""

    def __init__(self, names):
        self._names = list(names)
        self.calls = 0

    async def list_credential_names(self, user_id):
        self.calls += 1
        assert user_id == "u-test"
        return list(self._names)


def _run(payload, db):
    return asyncio.run(_enrich_interrupt_payload(payload, db, "u-test"))


def test_flags_missing_names_only():
    db = _StubDB(["HAVE_ONE"])
    payload = {
        "action_requests": [
            {
                "name": "run_file",
                "args": {
                    "code": "...",
                    "credentials": [
                        {"kind": "env_vars", "names": ["HAVE_ONE", "MISSING_A"]},
                    ],
                },
            }
        ]
    }
    result = _run(payload, db)
    assert result["missing_credentials"] == ["MISSING_A"]
    assert result["action_requests"][0]["missing_credentials"] == ["MISSING_A"]


def test_no_credentials_no_missing():
    db = _StubDB(["ANY"])
    payload = {"action_requests": [{"name": "foo", "args": {}}]}
    result = _run(payload, db)
    assert result["missing_credentials"] == []
    assert result["action_requests"][0]["missing_credentials"] == []


def test_dedupes_across_action_requests():
    # Same missing name referenced by two separate action_requests
    # appears exactly once in the top-level list, preserving first-seen
    # order.
    db = _StubDB([])
    payload = {
        "action_requests": [
            {"name": "t1", "args": {"credentials": [{"kind": "env_vars", "names": ["A", "B"]}]}},
            {"name": "t2", "args": {"credentials": [{"kind": "env_vars", "names": ["B", "C"]}]}},
        ]
    }
    result = _run(payload, db)
    assert result["missing_credentials"] == ["A", "B", "C"]


def test_file_kind_names_are_flagged():
    # ``file`` materializations carry their own ``names`` list; those must
    # also be checked against the store.
    db = _StubDB(["NASA_USER"])
    payload = {
        "action_requests": [
            {
                "name": "run_file",
                "args": {
                    "credentials": [
                        {
                            "kind": "file",
                            "path": "~/.netrc",
                            "names": ["NASA_USER", "NASA_TOKEN"],
                            "content": "machine x login {NASA_USER} password {NASA_TOKEN}\n",
                        }
                    ]
                },
            }
        ]
    }
    result = _run(payload, db)
    assert result["missing_credentials"] == ["NASA_TOKEN"]


def test_degrades_gracefully_on_db_failure():
    class _BrokenDB:
        async def list_credential_names(self, user_id):
            raise RuntimeError("db offline")

    payload = {"action_requests": [{"args": {"credentials": [{"kind": "env_vars", "names": ["X"]}]}}]}
    # With an unreachable DB we act as if the store is empty — the user sees
    # every referenced name flagged as missing. Better than raising and
    # dropping the interrupt payload entirely.
    result = _run(payload, _BrokenDB())
    assert result["missing_credentials"] == ["X"]


def test_non_dict_input_returns_safe_fallback():
    # Unknown shapes get a minimal payload rather than crashing the stream.
    db = _StubDB(["X"])
    result = _run("not-a-payload", db)
    assert "missing_credentials" in result
    assert result["missing_credentials"] == []


def test_malformed_action_requests_are_skipped():
    db = _StubDB([])
    payload = {
        "action_requests": [
            "not-a-dict",
            {"args": "not-a-dict"},
            {"args": {"credentials": "not-a-list"}},
            {"args": {"credentials": [{"kind": "env_vars", "names": ["WANTED"]}]}},
        ]
    }
    result = _run(payload, db)
    assert result["missing_credentials"] == ["WANTED"]
