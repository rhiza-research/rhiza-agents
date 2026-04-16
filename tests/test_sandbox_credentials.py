"""Tests for the credential resolution helpers in the sandbox tool.

These cover the pure functions that don't touch the Daytona SDK or the
async db: validation, name collection, and the env-vars/file build step.
"""

import pytest

from rhiza_agents.agents.tools.sandbox import (
    _build_runtime_injection,
    _collect_referenced_names,
    _daytona_sandbox_resources_from_env,
    _normalize_sandbox_upload_path,
    _validate_materializations,
)


def test_normalize_sandbox_upload_path_strips_tilde():
    """Daytona's fs.upload_file does not expand ``~``; we have to strip it."""
    assert _normalize_sandbox_upload_path("~/.netrc") == ".netrc"


def test_normalize_sandbox_upload_path_strips_home_prefix():
    """An absolute path under the sandbox home should land at the same place."""
    assert _normalize_sandbox_upload_path("/root/.netrc") == ".netrc"
    assert _normalize_sandbox_upload_path("/root/code/foo.py") == "code/foo.py"


def test_normalize_sandbox_upload_path_strips_leading_slash():
    """Absolute paths outside the sandbox home are made relative to it."""
    assert _normalize_sandbox_upload_path("/home/daytona/foo.py") == "home/daytona/foo.py"
    assert _normalize_sandbox_upload_path("/.netrc") == ".netrc"


def test_normalize_sandbox_upload_path_passes_relative_through():
    assert _normalize_sandbox_upload_path("code/foo.py") == "code/foo.py"
    assert _normalize_sandbox_upload_path(".netrc") == ".netrc"


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


def test_build_runtime_injection_skips_missing_env_vars():
    """Names absent from the secret store are silently dropped from the env
    dict (not raised, not substituted with placeholders). Lets a single skill
    list both required and optional credentials and have it Just Work for the
    user who only configured the required ones."""
    env, files = _build_runtime_injection(
        [{"kind": "env_vars", "names": ["NASA_USER", "NASA_PASS", "OPTIONAL_KEY"]}],
        {"NASA_USER": "alice", "NASA_PASS": "hunter2"},
    )
    assert env == {"NASA_USER": "alice", "NASA_PASS": "hunter2"}
    assert "OPTIONAL_KEY" not in env
    assert files == {}


def test_build_runtime_injection_drops_partially_resolvable_file():
    """A file materialization is dropped entirely if any of its referenced
    names is missing — a partially-rendered netrc is worse than no netrc."""
    env, files = _build_runtime_injection(
        [
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["U", "P"],
                "content": "machine x login {U} password {P}\n",
            }
        ],
        {"U": "alice"},  # P is missing
    )
    assert env == {}
    assert files == {}


def test_build_runtime_injection_keeps_other_materializations_when_one_drops():
    """Dropping an unresolvable file does not affect other materializations."""
    env, files = _build_runtime_injection(
        [
            {"kind": "env_vars", "names": ["A"]},
            {
                "kind": "file",
                "path": "~/.netrc",
                "names": ["U", "P"],
                "content": "machine x login {U} password {P}\n",
            },
            {"kind": "env_vars", "names": ["B"]},
        ],
        {"A": "1", "B": "2"},  # U and P missing
    )
    assert env == {"A": "1", "B": "2"}
    assert files == {}


def test_daytona_sandbox_resources_unset(monkeypatch):
    monkeypatch.delenv("DAYTONA_SANDBOX_DISK_GIB", raising=False)
    assert _daytona_sandbox_resources_from_env() is None


def test_daytona_sandbox_resources_empty(monkeypatch):
    monkeypatch.setenv("DAYTONA_SANDBOX_DISK_GIB", "")
    assert _daytona_sandbox_resources_from_env() is None


def test_daytona_sandbox_resources_invalid(monkeypatch):
    monkeypatch.setenv("DAYTONA_SANDBOX_DISK_GIB", "twenty")
    assert _daytona_sandbox_resources_from_env() is None


def test_daytona_sandbox_resources_non_positive(monkeypatch):
    monkeypatch.setenv("DAYTONA_SANDBOX_DISK_GIB", "0")
    assert _daytona_sandbox_resources_from_env() is None


def test_daytona_sandbox_resources_disk_gib(monkeypatch):
    pytest.importorskip("daytona_sdk")
    from daytona_sdk import Resources

    monkeypatch.setenv("DAYTONA_SANDBOX_DISK_GIB", "32")
    assert _daytona_sandbox_resources_from_env() == Resources(disk=32)
