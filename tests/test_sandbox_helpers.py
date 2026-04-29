"""Tests for the pure-ish helpers in tools/sandbox.py.

Coverage:
  - exec_as_daytona — that the su -l wrap actually wraps, that cwd is
    cd'd into inside the wrap (su -l resets the shell so external cwd
    won't propagate), and that env vars are exported inside the wrap.
  - drain_inotify_journal — that the parser aggregates events per path,
    picks the latest event as last_event, drops events outside the
    watched volumes, filters /skills/, and skips paths that no longer
    stat (deleted between event and drain).

These run without a real Daytona sandbox by mocking the SDK-facing
calls. The wrap correctness is the most security-relevant thing the
unit tests can pin down — it's what enforces non-root execution.
"""

import base64
import re
from types import SimpleNamespace
from unittest import mock

from rhiza_agents.agents.tools.sandbox import (
    drain_inotify_journal,
    exec_as_daytona,
)

# ---------------------------------------------------------------------------
# exec_as_daytona — the agent-execution privilege-drop wrapper
# ---------------------------------------------------------------------------


class _RecordingSandbox:
    """Sandbox stub that captures every process.exec call.

    Returns ``exit_code=0`` with empty result by default. Tests inspect
    ``self.exec_calls`` (the raw command strings) to verify wrapping
    behavior.
    """

    def __init__(self):
        self.exec_calls: list[tuple[str, dict]] = []

        class _P:
            def exec(p_self, cmd, **kwargs):  # noqa: N805 — match SDK
                self.exec_calls.append((cmd, kwargs))
                return SimpleNamespace(exit_code=0, result="")

        self.process = _P()


def _decode_inner(wrapped_cmd: str) -> str:
    """Pull the base64-encoded inner shell command back out of a wrap.

    The wrapper produces ``su -l daytona -c "echo <b64> | base64 -d | sh"``.
    The base64 alphabet is shell-safe so ``shlex.quote`` returns it
    unquoted; the regex below accepts that, plus the single-quoted
    form in case shlex's behavior changes.
    """
    match = re.search(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d \| sh", wrapped_cmd)
    assert match, f"could not find base64 payload in wrapped command: {wrapped_cmd!r}"
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_exec_as_daytona_wraps_with_su_l():
    sandbox = _RecordingSandbox()
    exec_as_daytona(sandbox, "ls /workspace")
    cmd, _ = sandbox.exec_calls[0]
    # The wrap is the only line of defense for non-root execution; verify
    # it actually fires for every call.
    assert cmd.startswith('su -l daytona -c "')
    assert "base64 -d | sh" in cmd
    inner = _decode_inner(cmd)
    assert inner == "ls /workspace"


def test_exec_as_daytona_cwd_injected_inside_wrap():
    sandbox = _RecordingSandbox()
    exec_as_daytona(sandbox, "pwd", cwd="/workspace")
    inner = _decode_inner(sandbox.exec_calls[0][0])
    # cwd has to be cd'd inside the wrapped shell because su -l starts
    # a fresh login shell at $HOME — the outer process.exec cwd kwarg
    # is not honored across the user switch.
    assert "cd /workspace" in inner
    assert "pwd" in inner
    # Order matters: cd before the command.
    assert inner.index("cd /workspace") < inner.index("pwd")


def test_exec_as_daytona_env_exports_inside_wrap():
    sandbox = _RecordingSandbox()
    exec_as_daytona(sandbox, "echo $FOO", env={"FOO": "bar"})
    inner = _decode_inner(sandbox.exec_calls[0][0])
    # env has to be exported inside the wrap because su -l resets the
    # environment to daytona's defaults.
    assert "export FOO=bar" in inner
    assert "echo $FOO" in inner


def test_exec_as_daytona_quotes_env_value_with_special_chars():
    sandbox = _RecordingSandbox()
    exec_as_daytona(sandbox, "echo $X", env={"X": "a; rm -rf /"})
    inner = _decode_inner(sandbox.exec_calls[0][0])
    # The value must be shell-quoted so the embedded `; rm -rf /`
    # doesn't get interpreted as a command separator.
    assert "export X='a; rm -rf /'" in inner


def test_exec_as_daytona_handles_command_with_quotes_and_dollars():
    """Commands the agent supplies may contain shell metacharacters that
    would otherwise be expanded twice (once by the outer su -c shell,
    once by the inner sh). The base64 round-trip prevents that."""
    sandbox = _RecordingSandbox()
    tricky = """python3 -c 'import os; print("$HOME")'"""
    exec_as_daytona(sandbox, tricky)
    inner = _decode_inner(sandbox.exec_calls[0][0])
    # Inner shell sees the original command literally — no expansion of
    # $HOME by the outer su -c shell.
    assert inner == tricky


def test_exec_as_daytona_cwd_and_env_compose():
    sandbox = _RecordingSandbox()
    exec_as_daytona(sandbox, "do_thing", cwd="/data", env={"K": "v"})
    inner = _decode_inner(sandbox.exec_calls[0][0])
    # All three components appear, in the right order: env exports, cd,
    # then the command.
    assert inner.index("export K=v") < inner.index("cd /data")
    assert inner.index("cd /data") < inner.index("do_thing")


# ---------------------------------------------------------------------------
# drain_inotify_journal — TSV parsing and path-source mapping
# ---------------------------------------------------------------------------


def _drain_with_responses(*responses: tuple[int, str], **kwargs):
    """Invoke drain_inotify_journal with ``exec_as_daytona`` returning the
    given (exit_code, result) responses in order.

    First call is the drain (cat then truncate); second is the stat
    batch over the surviving paths.
    """
    fake_responses = [SimpleNamespace(exit_code=ec, result=res) for ec, res in responses]
    with mock.patch(
        "rhiza_agents.agents.tools.sandbox.exec_as_daytona",
        side_effect=fake_responses,
    ) as patched:
        result = drain_inotify_journal(sandbox=object(), **kwargs)
    return result, patched


def test_drain_empty_journal_returns_empty_dict():
    result, _ = _drain_with_responses((0, ""))
    assert result == {}


def test_drain_aggregates_events_by_path_keeps_latest():
    journal = (
        "CREATE\t/workspace/foo.py\t1700000000\n"
        "MODIFY\t/workspace/foo.py\t1700000005\n"
        "CLOSE_WRITE\t/workspace/foo.py\t1700000010\n"
    )
    stat_out = "/workspace/foo.py|512|1700000010"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    assert "/foo.py" in result
    entry = result["/foo.py"]
    # last_event is whichever event had the highest timestamp.
    assert entry["last_event"] == "CLOSE_WRITE"
    assert entry["size"] == 512
    assert entry["source"] == "agent"  # default_source


def test_drain_workspace_paths_use_default_source():
    journal = "CREATE\t/workspace/a.py\t1700000000\n"
    stat_out = "/workspace/a.py|10|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out), default_source="output")
    assert result["/a.py"]["source"] == "output"


def test_drain_data_paths_always_use_data_source():
    """A /data path must always be labeled "data" regardless of the
    caller's default_source — write_file vs run_file vs skill calls
    all see the same label for shared-volume files.
    """
    journal = "CREATE\t/data/forecast.parquet\t1700000000\n"
    stat_out = "/data/forecast.parquet|999|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out), default_source="agent")
    # /data files keep their full path as the logical key.
    assert "/data/forecast.parquet" in result
    assert result["/data/forecast.parquet"]["source"] == "data"


def test_drain_filters_skills_paths():
    """Events on /skills/ are runtime plumbing and must not leak into
    state["files"] regardless of how they got recorded."""
    journal = "CREATE\t/skills/myskill/scripts/fetch.py\t1700000000\nCREATE\t/workspace/real.py\t1700000001\n"
    stat_out = "/workspace/real.py|10|1700000001"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    # Only the workspace path survives; skill path is filtered before stat.
    assert set(result.keys()) == {"/real.py"}


def test_drain_drops_paths_that_no_longer_exist():
    """A file may be created and deleted between the inotify event and
    the drain (a temp file, for example). When stat fails for that path
    the entry is dropped silently."""
    journal = (
        "CREATE\t/workspace/keeper.py\t1700000000\n"
        "CREATE\t/workspace/temp.py\t1700000001\n"
        "DELETE\t/workspace/temp.py\t1700000002\n"
    )
    # stat output only includes keeper.py — temp.py is gone.
    stat_out = "/workspace/keeper.py|42|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    assert set(result.keys()) == {"/keeper.py"}


def test_drain_ignores_lines_with_wrong_field_count():
    """Robustness: malformed journal lines (missing tabs, extra noise)
    are skipped rather than crashing the drain."""
    journal = (
        "CREATE\t/workspace/ok.py\t1700000000\n"
        "garbage line with no tabs\n"
        "TWO\tFIELDS\n"
        "FOUR\tFIELDS\there\there\n"  # 4 fields — splits to 4, not 3
        "CREATE\t/workspace/also-ok.py\tNOT_A_NUMBER\n"  # bad ts
    )
    stat_out = "/workspace/ok.py|1|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    assert set(result.keys()) == {"/ok.py"}


def test_drain_drops_events_outside_watched_areas():
    """Inotify shouldn't watch outside /workspace and /data, but if a
    rogue event reaches the journal (manual daemon, etc.), the drain's
    source-mapping rejects it."""
    journal = "CREATE\t/etc/passwd\t1700000000\nCREATE\t/workspace/legit.py\t1700000001\n"
    # stat over both paths; even if /etc/passwd exists, drain drops it
    # at the source-mapping step.
    stat_out = "/workspace/legit.py|10|1700000001\n/etc/passwd|2000|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    assert set(result.keys()) == {"/legit.py"}


def test_drain_workspace_root_collapses_to_slash():
    """The /workspace mount point itself is mapped to logical path "/"
    so it doesn't collide with workspace contents."""
    journal = "CREATE\t/workspace\t1700000000\n"
    stat_out = "/workspace|0|1700000000"
    result, _ = _drain_with_responses((0, journal), (0, stat_out))
    assert "/" in result


def test_drain_failed_drain_exec_returns_empty():
    """If the drain command itself errors (sandbox unreachable, etc.),
    return an empty dict rather than blowing up the calling tool."""
    result, _ = _drain_with_responses((1, ""))
    assert result == {}
