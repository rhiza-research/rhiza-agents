"""Tests for run_file's shell-safe CLI argument passthrough.

Covers the pure command-builder helper. The rest of run_file hits the
Daytona sandbox so isn't exercised here.
"""

import shlex

import pytest

from rhiza_agents.agents.tools.files import _build_uv_run_cmd


def test_no_args_produces_bare_uv_run():
    assert _build_uv_run_cmd("script.py", None) == "uv run script.py"


def test_empty_list_treated_as_no_args():
    assert _build_uv_run_cmd("script.py", []) == "uv run script.py"


def test_simple_args_appended():
    cmd = _build_uv_run_cmd("script.py", ["--date", "2026-02-15", "--region", "kenya"])
    assert cmd == "uv run script.py --date 2026-02-15 --region kenya"


def test_value_with_space_is_quoted():
    cmd = _build_uv_run_cmd("p.py", ["--title", "hello world"])
    # shlex.quote will wrap the value in single quotes.
    assert cmd == "uv run p.py --title 'hello world'"
    # And the quoted form parses back to the original token.
    tokens = shlex.split(cmd)
    assert tokens == ["uv", "run", "p.py", "--title", "hello world"]


def test_value_with_single_quote_is_escaped():
    cmd = _build_uv_run_cmd("p.py", ["--name", "it's fine"])
    tokens = shlex.split(cmd)
    assert tokens[-1] == "it's fine"


def test_path_with_spaces_quoted():
    cmd = _build_uv_run_cmd("p.py", ["--out", "/tmp/a b/c.png"])
    tokens = shlex.split(cmd)
    assert tokens[-2:] == ["--out", "/tmp/a b/c.png"]


@pytest.mark.parametrize(
    "hazard",
    [
        "*.nc",
        "; rm -rf /",
        "`whoami`",
        "$(cat /etc/passwd)",
        "foo && bar",
        "foo | bar",
        "< /etc/shadow",
        "> /tmp/evil",
    ],
)
def test_shell_metacharacters_are_not_interpreted(hazard):
    """Dangerous shell chars must pass through as literal argv tokens."""
    cmd = _build_uv_run_cmd("p.py", ["--arg", hazard])
    tokens = shlex.split(cmd)
    # The hazardous value must survive intact as a single token.
    assert tokens[-1] == hazard
    # And must be quoted (not a bare unquoted char).
    assert shlex.quote(hazard) in cmd


def test_filename_with_space_not_quoted_by_us():
    """We don't quote the filename because it comes from our own path
    normalization, not user input. Document that contract in a test."""
    # If this ever breaks, filename must also be shlex.quote()'d to keep
    # the invariant. Currently our filenames come from
    # _normalize_sandbox_upload_path which strips leading slashes and
    # never produces spaces.
    cmd = _build_uv_run_cmd("code/ok.py", None)
    assert cmd == "uv run code/ok.py"
