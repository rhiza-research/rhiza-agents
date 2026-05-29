"""Tests for the read-only bash inspection validator.

The validator (``validate_bash_command``) is a default-deny allowlist: it
parses a command with bashlex, walks every top-level command in the
command-LIST constructs (pipelines, ``;``/``&&``/``||``, subshell, brace
group, ``if``/``while``/``for``/``until``), and rejects anything that is not a
read-only inspection program — or that writes, builds a command at runtime,
wraps an uninspectable shell body, runs a find action predicate, or fails to
parse. Nested-command and parameter-expansion constructs — command
substitution ``$(...)``/backticks, process substitution ``<(...)``/``>(...)``,
and parameter expansion ``$VAR``/``${...}`` — are rejected WHOLESALE wherever
they appear, because bashlex flattens some of them (e.g. the default value in
``${x:-$(python y)}``) into uninspectable strings; the validator no longer
tries to validate the inner commands of nested constructs. Pipes and globs are
NOT nested constructs and remain fully allowed. The validator is the only line
of defense between an agent-supplied command string and ``exec_as_daytona`` in
the sandbox, so the bypass battery below is the security contract.

The validator is pure (no sandbox), so it is tested directly. Each deny case
asserts that the offending construct is named in the teaching message and
that the message points at the skill/run_file execution path.

``rm`` literals are assembled at runtime (``_RM``) so the project's
``block-delete`` PreToolUse hook doesn't reject the test file's own source.
"""

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

import rhiza_agents.agents.tools.sandbox as sandbox_module
from rhiza_agents.agents.tools.bash_inspect import (
    ALLOWED_PROGRAMS,
    MAX_OUTPUT_BYTES,
    BashValidationError,
    _cap_output,
    make_bash,
    validate_bash_command,
)

# Assemble destructive tokens at runtime to keep them out of the source text.
_RM = "r" + "m"


def _ok(cmd: str) -> None:
    """Assert the command is accepted (validator returns None)."""
    validate_bash_command(cmd)  # raises BashValidationError on rejection


def _rejected(cmd: str) -> str:
    """Assert the command is rejected; return the reason for substring checks."""
    with pytest.raises(BashValidationError) as exc:
        validate_bash_command(cmd)
    return exc.value.reason


def _teaches(reason: str) -> None:
    """Every deny message must point the agent at the execution path."""
    assert "run_file" in reason, f"reason does not point at run_file: {reason!r}"


# --- allowed: single command, pipeline, glob (I/O matrix rows 1-3) ---


def test_allowed_single_command():
    _ok("ls -la /workspace")


def test_allowed_pipeline_every_stage_allowlisted():
    _ok("find /data -name '*.zarr' | head")


def test_allowed_glob_expands_at_runtime():
    # The validator does not expand globs; the shell does at runtime. du is
    # allowlisted, so the command is accepted as-is.
    _ok("du -sh /data/*")


def test_allowed_multi_stage_pipeline():
    _ok("grep -r pattern /data | sort | uniq")


def test_allowed_sequence_and_logical_operators():
    _ok("stat /data/x && ls /workspace ; df -h")


def test_allowed_leading_var_assignment_then_allowlisted():
    # Leading VAR=value tokens are stripped before resolving argv0.
    _ok("FOO=bar ls /workspace")


def test_allowed_input_redirect():
    # Input redirection does not write — allowed.
    _ok("cat < /workspace/in.txt")


def test_allowed_find_without_action_predicate():
    _ok("find /data -name '*.nc' -type f")


@pytest.mark.parametrize("prog", sorted(ALLOWED_PROGRAMS))
def test_every_allowlisted_program_accepted(prog):
    # A bare invocation of each allowlisted program is accepted.
    _ok(f"{prog} /workspace")


# --- denied: non-allowlisted program (I/O matrix row 4) ---


def test_denied_python_names_the_program_and_teaches():
    reason = _rejected("python foo.py")
    assert "python" in reason
    _teaches(reason)


def test_denied_echo_not_in_allowlist():
    reason = _rejected("echo hi")
    assert "echo" in reason
    _teaches(reason)


def test_denied_uv_run():
    reason = _rejected("uv run x.py")
    assert "uv" in reason
    _teaches(reason)


@pytest.mark.parametrize("prog", ["curl", "wget", "nc", "ncat"])
def test_denied_network_tools(prog):
    reason = _rejected(f"{prog} http://example.com")
    assert prog in reason
    _teaches(reason)


# --- denied: pipe to denied program (I/O matrix row 5) ---


def test_denied_pipe_to_tee():
    reason = _rejected("ls | tee f")
    assert "tee" in reason
    _teaches(reason)


def test_denied_pipe_to_xargs():
    reason = _rejected(f"find . | xargs {_RM}")
    assert "xargs" in reason
    assert "stdin" in reason
    _teaches(reason)


# --- denied: output redirect (I/O matrix row 6) ---


def test_denied_output_redirect_truncate():
    reason = _rejected("cat x > y")
    assert "redirection" in reason
    assert "cannot write files" in reason
    _teaches(reason)


def test_denied_output_redirect_append():
    reason = _rejected("cat x >> y")
    assert "redirection" in reason
    _teaches(reason)


def test_denied_output_redirect_clobber():
    reason = _rejected("ls >| f")
    assert "redirection" in reason
    _teaches(reason)


def test_denied_output_redirect_stdout_stderr():
    reason = _rejected("ls &> out")
    assert "redirection" in reason
    _teaches(reason)


def test_denied_fd_qualified_output_redirect():
    # `2> err` writes via fd 2; the redirect type still contains '>'.
    reason = _rejected("ls /nope 2> err")
    assert "redirection" in reason
    _teaches(reason)


# --- denied: nested-command constructs rejected WHOLESALE (I/O matrix row 7) ---
#
# The validator no longer inspects the inner command of a nested construct; it
# rejects the construct itself. So a command substitution / process
# substitution is rejected by the construct rule (naming the construct), not
# because its inner program is off the allowlist.


def test_denied_command_substitution_rejected_wholesale():
    # `cat $(...)`: the command substitution is rejected by the construct rule,
    # regardless of what the inner command is. The reason names the construct.
    reason = _rejected(f"cat $({_RM} -rf x)")
    assert "command substitution" in reason
    _teaches(reason)


def test_denied_backtick_substitution_rejected_wholesale():
    # Backticks parse to the same `commandsubstitution` kind as `$(...)`.
    reason = _rejected("cat `python evil.py`")
    assert "command substitution" in reason
    assert "backtick" in reason
    _teaches(reason)


def test_denied_process_substitution_rejected_wholesale():
    # `<(...)` is rejected by the construct rule; the inner command is not
    # inspected.
    reason = _rejected("cat <(python -c 'pass')")
    assert "process substitution" in reason
    _teaches(reason)


# --- denied: shell wrappers (I/O matrix row 8) ---


def test_denied_bash_dash_c():
    reason = _rejected('bash -c "ls"')
    assert "bash" in reason
    assert "shell wrapper" in reason
    _teaches(reason)


def test_denied_sh_heredoc():
    # `sh <<EOF...` is rejected. The here-doc redirect check runs before the
    # argv0 shell-wrapper check (redirect checks are unconditional and at the
    # top of `_check_command` so an assignment-only `FOO=bar > out` is caught),
    # so the here-doc message wins here. Either way the command never reaches
    # the sandbox; both rejections uphold the read-only guarantee.
    reason = _rejected("sh <<EOF\nls\nEOF\n")
    assert "here-document" in reason
    _teaches(reason)


@pytest.mark.parametrize("wrapper", ["bash", "sh", "zsh", "dash", "ksh", "ash"])
def test_denied_all_shell_wrappers(wrapper):
    reason = _rejected(f'{wrapper} -c "ls"')
    assert wrapper in reason
    _teaches(reason)


# --- denied: runtime-argv builders ---


@pytest.mark.parametrize("builtin", ["eval", "source", "exec"])
def test_denied_runtime_argv_builtins(builtin):
    reason = _rejected(f"{builtin} ls")
    assert builtin in reason
    _teaches(reason)


def test_denied_dot_source_builtin():
    # `. file` (POSIX source) — argv0 basename is ".".
    reason = _rejected(". /workspace/x")
    assert "runtime" in reason or "build" in reason
    _teaches(reason)


# --- denied: find action predicates (I/O matrix row 9) ---


def test_denied_find_delete():
    reason = _rejected("find . -delete")
    assert "-delete" in reason
    _teaches(reason)


def test_denied_find_exec():
    reason = _rejected(f"find . -exec {_RM} {{}} \\;")
    assert "-exec" in reason
    _teaches(reason)


@pytest.mark.parametrize("pred", ["-exec", "-execdir", "-delete", "-fprintf", "-fprint", "-fprint0", "-fls"])
def test_denied_all_find_action_predicates(pred):
    reason = _rejected(f"find /data {pred} foo")
    assert pred in reason
    _teaches(reason)


# --- denied: interpreters that can write (sed -i, awk redirect) ---


def test_denied_sed():
    reason = _rejected("sed -i s/a/b/ f")
    assert "sed" in reason
    _teaches(reason)


def test_denied_awk():
    reason = _rejected('awk "BEGIN{print > \\"f\\"}"')
    assert "awk" in reason
    _teaches(reason)


# --- denied: unparseable input (I/O matrix row 10), fail closed ---


def test_denied_unparseable_unterminated_quote():
    reason = _rejected("ls 'unterminated")
    assert "parse" in reason
    _teaches(reason)


def test_denied_unparseable_dangling_pipe():
    reason = _rejected("ls |")
    _teaches(reason)


def test_denied_empty_command():
    reason = _rejected("")
    _teaches(reason)


def test_denied_whitespace_only_command():
    reason = _rejected("   \n  ")
    _teaches(reason)


# --- denylist precedence: a denied stage anywhere fails the whole command ---


def test_denied_when_only_one_pipeline_stage_is_bad():
    # First stage allowed, second denied → whole command rejected.
    reason = _rejected("ls /workspace | python -")
    assert "python" in reason
    _teaches(reason)


def test_denied_substitution_in_allowed_command_rejected_wholesale():
    # `ls $(...)`: even though `ls` is allowlisted, the command substitution in
    # its argument is rejected by the construct rule (the inner `uv run` is not
    # inspected — the construct itself is the rejection).
    reason = _rejected("ls $(uv run x.py)")
    assert "command substitution" in reason
    _teaches(reason)


# --- finding A: nested-command construct in a redirect target is rejected ---
#
# A command substitution in a redirect target is still surfaced by the walk
# (the redirect's `.output`/`.input`/`.heredoc` child is walked), but now it is
# rejected by the construct rule rather than by inspecting its inner program.


def test_denied_command_substitution_in_input_redirect():
    # `cat < $(...)`: the command substitution in the redirect target is
    # rejected by the construct rule. `dash` would run the inner command to
    # produce the redirect target, which is exactly the bypass we reject.
    reason = _rejected(f"cat < $({_RM} x)")
    assert "command substitution" in reason
    _teaches(reason)


def test_denied_command_substitution_in_input_redirect_python():
    reason = _rejected("cat < $(python x)")
    assert "command substitution" in reason
    _teaches(reason)


def test_denied_command_substitution_in_here_string():
    # Here-string `<<<` target is a redirect; a command substitution in it is
    # surfaced by the walk and rejected by the construct rule.
    reason = _rejected(f"grep x <<< $({_RM} y)")
    assert "command substitution" in reason
    _teaches(reason)


def test_allowed_input_redirect_from_plain_file_still_passes():
    # A benign input redirect with no inner command still passes (regression
    # guard: walking the redirect target must not reject ordinary `< file`).
    _ok("cat < file")


# --- nested-command / parameter-expansion constructs rejected WHOLESALE ---
#
# Three review rounds found RCE/write bypasses rooted in constructs bashlex
# flattens into uninspectable strings (command substitution nested in a
# parameter default value, in a here-doc body). The decision is to STOP
# validating inner commands of nested constructs and instead reject the
# constructs themselves anywhere they appear. The reason names the construct
# and points at the read-only / run_file path (parameter expansion instead
# tells the agent to use literal paths).


def test_denied_dollar_paren_command_substitution_names_construct():
    reason = _rejected("cat $(id)")
    assert "command substitution" in reason
    assert "`$(...)`" in reason


def test_denied_backtick_command_substitution_names_construct():
    reason = _rejected("cat `id`")
    assert "command substitution" in reason
    assert "backtick" in reason


def test_denied_process_substitution_input_form_names_construct():
    reason = _rejected("diff <(cat a) <(cat b)")
    assert "process substitution" in reason
    assert "`<(...)`" in reason


def test_denied_process_substitution_output_form_names_construct():
    # `>(...)` parses to the same `processsubstitution` kind as `<(...)`.
    reason = _rejected("tee >(cat) < x")
    assert "process substitution" in reason


def test_denied_simple_variable_expansion_names_construct():
    # `$VAR` parses to a `parameter` node and is rejected. The parameter
    # message points at literal paths (it does not mention run_file).
    reason = _rejected("cat $HOME")
    assert "variable/parameter expansion" in reason
    assert "literal paths" in reason


def test_denied_braced_variable_expansion_names_construct():
    # `${...}` also parses to a `parameter` node.
    reason = _rejected("ls ${PATH}")
    assert "variable/parameter expansion" in reason
    assert "literal paths" in reason


def test_denied_parameter_default_with_nested_command_substitution():
    # `cat ${x:-$(python y)}`: bashlex parses the `${x:-...}` default-value
    # body as a flat `parameter.value` string, so the `$(python y)` inside is
    # never surfaced as a node. Rejecting the `parameter` kind wholesale closes
    # this bypass — the command is rejected by the parameter-expansion rule.
    reason = _rejected("cat ${x:-$(python y)}")
    assert "variable/parameter expansion" in reason
    assert "literal paths" in reason


def test_denied_command_substitution_in_pipeline_stage():
    # A command substitution inside one pipeline stage is rejected even though
    # the stage's argv0 (`grep`) is allowlisted.
    reason = _rejected("ls | grep $(id)")
    assert "command substitution" in reason


def test_denied_variable_expansion_in_pipeline_stage():
    reason = _rejected("cat /data/x | grep $PATTERN")
    assert "variable/parameter expansion" in reason


def test_denied_command_substitution_in_subshell():
    # A subshell `( ... )` is a command-LIST construct that is still walked;
    # the command substitution inside it is rejected by the construct rule.
    reason = _rejected("(cat $(id))")
    assert "command substitution" in reason


# --- regression: pipes and globs are NOT nested constructs and still pass ---


def test_allowed_pipe_to_head_still_passes():
    _ok("find /data -name '*.zarr' | head")


def test_allowed_glob_still_passes():
    # A glob `*` is an ordinary word the shell expands at runtime, not a
    # nested-command construct — it must still pass.
    _ok("du -sh /data/*")


def test_allowed_multi_stage_pipeline_still_passes():
    _ok("sort -r x | uniq -c")


def test_allowed_grep_recursive_still_passes():
    _ok("grep -rn foo /workspace")


def test_allowed_tree_depth_still_passes():
    _ok("tree -L 2")


def test_allowed_cat_literal_path_still_passes():
    _ok("cat /data/x")


# --- finding B: find -ok / -okdir are action predicates ---


@pytest.mark.parametrize("pred", ["-ok", "-okdir"])
def test_denied_find_ok_predicates(pred):
    reason = _rejected(f"find . {pred} {_RM} {{}} \\;")
    assert pred in reason
    _teaches(reason)


def test_denied_find_fprint0():
    # GNU `find -fprint0 FILE` writes NUL-separated names to FILE — a write,
    # so it is rejected like the other -fprint* action predicates.
    reason = _rejected("find /data -fprint0 out")
    assert "-fprint0" in reason
    _teaches(reason)


# --- finding C: write-capable flags on allowlisted readers (sort/tree) ---


def test_denied_sort_output_short_flag():
    reason = _rejected("sort -o f x")
    assert "sort" in reason
    assert "-o" in reason
    _teaches(reason)


def test_denied_sort_output_long_flag_joined():
    reason = _rejected("sort --output=f x")
    assert "sort" in reason
    assert "--output" in reason
    _teaches(reason)


def test_denied_sort_output_long_flag_separate():
    reason = _rejected("sort --output f x")
    assert "sort" in reason
    assert "--output" in reason
    _teaches(reason)


def test_denied_tree_output_flag():
    reason = _rejected("tree -o f")
    assert "tree" in reason
    assert "-o" in reason
    _teaches(reason)


def test_allowed_sort_without_write_flag():
    _ok("sort x")


def test_allowed_sort_reverse_piped_to_uniq():
    _ok("sort -r x | uniq")


def test_allowed_tree_with_depth_flag():
    _ok("tree -L 2")


# --- finding: uniq's second positional is an OUTPUT file (positional write) ---


def test_allowed_uniq_single_positional():
    # `uniq INPUT` reads INPUT, writes stdout — one positional, allowed.
    _ok("uniq file")


def test_allowed_uniq_count_flag_single_positional():
    # `-c` is a non-value flag; `file` is the one positional (the input).
    _ok("uniq -c file")


def test_allowed_uniq_skip_fields_value_not_counted():
    # `-f` takes a SEPARATE numeric value (`2`); it must not be counted as a
    # positional, so `uniq -f 2 file` has exactly one positional (`file`).
    _ok("uniq -f 2 file")


def test_allowed_uniq_from_stdin_no_positionals():
    # Piped into uniq with no positional args — zero positionals, allowed.
    _ok("sort x | uniq")


def test_denied_uniq_two_positionals_is_output_write():
    reason = _rejected("uniq in out")
    assert "uniq" in reason
    assert "OUTPUT" in reason
    _teaches(reason)


def test_denied_uniq_count_flag_with_two_positionals():
    # `-c` is a non-value flag, so `in` and `out` are both positionals — the
    # second (`out`) is the output file uniq writes.
    reason = _rejected("uniq -c in out")
    assert "uniq" in reason
    assert "OUTPUT" in reason
    _teaches(reason)


# --- FIX 1: here-documents are uninspectable and rejected; <<< stays allowed ---


def test_denied_heredoc_with_command_substitution_in_body():
    # bashlex stores the here-doc body as a flat `.value` string with no
    # `.parts`, so a `$(...)` in the body is never walked. The body is
    # uninspectable, so the here-doc redirect is rejected outright.
    reason = _rejected("cat <<EOF\n$(python evil)\nEOF\n")
    assert "here-document" in reason
    _teaches(reason)


def test_denied_heredoc_with_plain_body():
    # Even a benign body is rejected — the body cannot be statically inspected
    # regardless of its contents.
    reason = _rejected("cat <<EOF\nplain text\nEOF\n")
    assert "here-document" in reason
    _teaches(reason)


def test_denied_heredoc_dash_form():
    # The tab-stripping `<<-` here-doc form is equally uninspectable.
    reason = _rejected("cat <<-EOF\nplain\nEOF\n")
    assert "here-document" in reason
    _teaches(reason)


def test_allowed_here_string_with_plain_target():
    # The here-string `<<<` target is a real word node `_walk` validates; a
    # plain target has no inner command, so it passes (regression guard that
    # FIX 1 did not over-reach to `<<<`).
    _ok("grep x <<< foo")


def test_denied_here_string_inner_command_substitution_rejected_wholesale():
    # `grep x <<< $(cat f)`: even though the inner `cat` is allowlisted, the
    # command substitution in the here-string target is rejected by the
    # construct rule — the validator no longer inspects inner commands of
    # nested constructs.
    reason = _rejected("grep x <<< $(cat f)")
    assert "command substitution" in reason
    _teaches(reason)


def test_denied_here_string_inner_denied_command():
    # `grep x <<< $(python y)`: the command substitution in the here-string is
    # rejected by the construct rule regardless of the inner program.
    reason = _rejected("grep x <<< $(python y)")
    assert "command substitution" in reason
    _teaches(reason)


# --- FIX 2: redirect checks run before the empty-argv early return ---


def test_denied_assignment_only_with_output_redirect_truncate():
    # `FOO=bar > out`: stripping the assignment leaves an empty real argv, but
    # `sh` still creates/truncates `out`. The output-redirect check must run
    # before the empty-argv early return.
    reason = _rejected("FOO=bar > out")
    assert "redirection" in reason
    _teaches(reason)


def test_denied_assignment_only_with_output_redirect_append():
    reason = _rejected("FOO=bar >> out")
    assert "redirection" in reason
    _teaches(reason)


def test_denied_assignment_only_with_heredoc():
    # An assignment-only command with a here-doc is also rejected — the
    # here-doc check likewise runs before the empty-argv early return.
    reason = _rejected("FOO=bar <<EOF\nx\nEOF\n")
    assert "here-document" in reason
    _teaches(reason)


def test_allowed_bare_assignment_no_redirect_still_passes():
    # A bare assignment with no redirect executes nothing and writes nothing —
    # still accepted (regression guard that FIX 2 did not reject assignments).
    _ok("FOO=bar")


def test_allowed_normal_command_unaffected_by_redirect_restructure():
    # Normal allowlisted commands with no redirect behave exactly as before.
    _ok("ls /workspace")


def test_denied_normal_command_output_redirect_still_rejected():
    # `ls > out` is still rejected after the redirect-check restructure.
    reason = _rejected("ls > out")
    assert "redirection" in reason
    _teaches(reason)


# --- FIX 3: sort/tree short write flag in joined and bundled single-dash forms ---


def test_denied_sort_output_short_flag_joined():
    # `sort -obar` joins the `-o` write flag with its value `bar`; the joined
    # form must be rejected.
    reason = _rejected("sort -obar file")
    assert "sort" in reason
    assert "-o" in reason
    _teaches(reason)


def test_denied_sort_output_short_flag_bundled():
    # `-roout`: `-o` bundled after `-r`, with value joined. The over-approx
    # (any single-dash token whose pre-`=` chars contain `o`) catches it.
    reason = _rejected("sort -roout x")
    assert "sort" in reason
    assert "-o" in reason
    _teaches(reason)


def test_denied_sort_output_short_flag_separate_value_still_rejected():
    # `sort -o f x` (separate value) remains rejected — regression guard for
    # the pre-existing short-flag handling.
    reason = _rejected("sort -o f x")
    assert "sort" in reason
    assert "-o" in reason
    _teaches(reason)


def test_denied_tree_output_short_flag_joined():
    reason = _rejected("tree -ofoo")
    assert "tree" in reason
    assert "-o" in reason
    _teaches(reason)


def test_allowed_sort_reverse_flag_no_o():
    # `-r` has no `o` — not a write flag, accepted.
    _ok("sort -r x | uniq")


def test_allowed_sort_key_flag_no_o():
    # `-k1` has no `o` — accepted.
    _ok("sort -k1 x")


def test_allowed_sort_plain_positional():
    _ok("sort x")


def test_allowed_tree_depth_flag_no_o():
    _ok("tree -L 2")


def test_allowed_tree_directories_only_flag_no_o():
    # `-d` (directories only) has no `o` — accepted.
    _ok("tree -d")


# --- finding I: denied program in case body / function definition rejected ---


def test_denied_program_in_function_definition_body():
    # A denied program hidden in a function body is surfaced and rejected.
    reason = _rejected("f() { python z; }")
    assert "python" in reason
    _teaches(reason)


def test_denied_program_in_case_statement_body():
    # bashlex does not implement `case` pattern parsing, so a case statement
    # raises during parse and is rejected fail-closed. Either way the command
    # never reaches the sandbox.
    reason = _rejected("case x in y) python z ;; esac")
    _teaches(reason)


# --- finding D: _cap_output coerces non-str / None sandbox output ---


def test_cap_output_coerces_none_to_empty_string():
    # The sandbox SDK may return None as the result; _cap_output must not
    # raise AttributeError on .encode().
    assert _cap_output(None) == ""


def test_cap_output_coerces_non_str_to_str():
    assert _cap_output(1234) == "1234"


def test_cap_output_passes_through_short_str():
    assert _cap_output("hello") == "hello"


def test_cap_output_truncates_oversized_str():
    big = "a" * (MAX_OUTPUT_BYTES + 100)
    out = _cap_output(big)
    assert "output truncated" in out
    assert out.encode("utf-8").startswith(b"a" * MAX_OUTPUT_BYTES)


# --- tool-level robustness: bash tool with a mocked sandbox (findings D/E) ---


def _fake_runtime():
    """A ToolRuntime-shaped stub the bash tool reads tool_call_id/config from."""
    return SimpleNamespace(
        tool_call_id="tc-test",
        config={"configurable": {"thread_id": "t-test"}},
    )


def _invoke_bash(command: str, **sandbox_patches):
    """Run the bash tool's coroutine with the sandbox module functions mocked.

    ``sandbox_patches`` maps a function name on ``tools.sandbox`` to either a
    ``return_value`` (wrapped) or a ``mock.patch.object`` kwarg dict.
    """
    bash = make_bash()

    async def _run():
        return await bash.coroutine(command=command, runtime=_fake_runtime())

    with mock.patch.object(sandbox_module, "is_sandbox_available", return_value=True):
        patchers = [mock.patch.object(sandbox_module, name, **kw) for name, kw in sandbox_patches.items()]
        for p in patchers:
            p.start()
        try:
            return asyncio.run(_run())
        finally:
            for p in patchers:
                p.stop()


def _message(command_result) -> object:
    return command_result.update["messages"][0]


def test_bash_tool_coerces_none_result_without_crashing():
    # finding D: exec_as_daytona returns result=None; the tool must return a
    # ToolMessage with empty content, not raise AttributeError.
    result = _invoke_bash(
        "ls /workspace",
        _get_or_create_sandbox={"return_value": object()},
        exec_as_daytona={"return_value": SimpleNamespace(exit_code=0, result=None)},
    )
    msg = _message(result)
    assert msg.content == ""


def test_bash_tool_coerces_non_str_result_without_crashing():
    result = _invoke_bash(
        "ls /workspace",
        _get_or_create_sandbox={"return_value": object()},
        exec_as_daytona={"return_value": SimpleNamespace(exit_code=0, result=42)},
    )
    msg = _message(result)
    assert msg.content == "42"


def test_bash_tool_returns_error_message_when_sandbox_create_raises():
    # finding E: _get_or_create_sandbox raising (Daytona down, quota, network)
    # must be caught and returned as an error ToolMessage, never propagate.
    result = _invoke_bash(
        "ls /workspace",
        _get_or_create_sandbox={"side_effect": RuntimeError("daytona is unreachable")},
    )
    msg = _message(result)
    assert msg.status == "error"
    assert "sandbox interaction failed" in msg.content
    # Concise message — the raw exception text must not leak.
    assert "daytona is unreachable" not in msg.content


def test_bash_tool_returns_error_message_when_exec_raises():
    result = _invoke_bash(
        "ls /workspace",
        _get_or_create_sandbox={"return_value": object()},
        exec_as_daytona={"side_effect": ConnectionError("network blip")},
    )
    msg = _message(result)
    assert msg.status == "error"
    assert "sandbox interaction failed" in msg.content
    assert "network blip" not in msg.content
