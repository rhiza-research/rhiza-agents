"""Tests for the credential resolution helpers in the sandbox tool.

These cover the pure functions that don't touch the Daytona SDK or the
async db: validation, name collection, and the env-vars/file build step.
"""

from rhiza_agents.agents.tools.sandbox import (
    _build_runtime_injection,
    _collect_referenced_names,
    _validate_materializations,
)


def test_validate_rejects_non_list():
    assert _validate_materializations("nope") is not None


def test_validate_accepts_empty_list():
    assert _validate_materializations([]) is None


def test_validate_env_vars_shape():
    assert _validate_materializations([{"kind": "env_vars", "names": ["A", "B"]}]) is None
    assert _validate_materializations([{"kind": "env_vars", "names": "A"}]) is not None
    assert _validate_materializations([{"kind": "env_vars", "names": [""]}]) is not None
    assert _validate_materializations([{"kind": "env_vars", "names": []}]) is not None
    assert _validate_materializations([{"kind": "env_vars"}]) is not None


def test_validate_file_shape():
    ok = [
        {
            "kind": "file",
            "path": "~/.netrc",
            "names": ["U"],
            "content": "machine x login {U}",
        }
    ]
    assert _validate_materializations(ok) is None
    # missing path
    assert _validate_materializations([{"kind": "file", "path": "", "names": ["U"], "content": "x"}]) is not None
    # missing content
    assert _validate_materializations([{"kind": "file", "path": "p", "names": ["U"]}]) is not None
    # missing names
    assert _validate_materializations([{"kind": "file", "path": "p", "content": "x"}]) is not None


def test_validate_file_rejects_undeclared_placeholder():
    """Placeholders in content must also appear in the explicit names list."""
    bad = [
        {
            "kind": "file",
            "path": "~/.netrc",
            "names": ["U"],
            "content": "machine x login {U} password {P}",
        }
    ]
    err = _validate_materializations(bad)
    assert err is not None
    assert "P" in err


def test_validate_unknown_kind():
    assert _validate_materializations([{"kind": "exec_arbitrary", "names": ["X"]}]) is not None


def test_collect_names_env_vars():
    names = _collect_referenced_names([{"kind": "env_vars", "names": ["A", "B", "A"]}])
    assert names == ["A", "B"]


def test_collect_names_file_uses_explicit_list():
    """file kind's collected names come from the explicit `names` list, not
    from regexing the content template."""
    names = _collect_referenced_names(
        [
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["U", "P"],
                "content": "{U} and {P}",
            }
        ]
    )
    assert names == ["U", "P"]


def test_collect_names_mixed():
    names = _collect_referenced_names(
        [
            {"kind": "env_vars", "names": ["TAHMO_USER", "TAHMO_PASS"]},
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["NASA_USER", "NASA_PASS"],
                "content": "machine x login {NASA_USER} password {NASA_PASS}",
            },
        ]
    )
    assert names == ["TAHMO_USER", "TAHMO_PASS", "NASA_USER", "NASA_PASS"]


def test_build_runtime_injection_env_vars():
    env, files = _build_runtime_injection(
        [{"kind": "env_vars", "names": ["NASA_USER", "NASA_PASS"]}],
        {"NASA_USER": "alice", "NASA_PASS": "hunter2"},
    )
    assert env == {"NASA_USER": "alice", "NASA_PASS": "hunter2"}
    assert files == {}


def test_build_runtime_injection_file_substitution():
    env, files = _build_runtime_injection(
        [
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["U", "P"],
                "content": "machine x login {U} password {P}\n",
            }
        ],
        {"U": "alice", "P": "hunter2"},
    )
    assert env == {}
    assert files == {"~/.netrc": "machine x login alice password hunter2\n"}


def test_build_runtime_injection_concatenates_same_path():
    """Two file materializations targeting the same path get concatenated
    in the order the LLM listed them. This is how scripts that need
    multiple netrc machine entries work."""
    env, files = _build_runtime_injection(
        [
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["AU", "AP"],
                "content": "machine a login {AU} password {AP}\n",
            },
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["BU", "BP"],
                "content": "machine b login {BU} password {BP}\n",
            },
        ],
        {"AU": "alice", "AP": "p1", "BU": "bob", "BP": "p2"},
    )
    assert env == {}
    assert files == {"~/.netrc": "machine a login alice password p1\nmachine b login bob password p2\n"}
