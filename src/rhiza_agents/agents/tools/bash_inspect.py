"""Read-only inspection ``bash`` tool for the Daytona sandbox.

The agent can execute trusted skill scripts via ``run_file`` but otherwise
cannot look at the sandbox filesystem. That blocks the inspect-and-chain
workflow (list files, search, check sizes/ages, peek at contents) the agent
needs to reason about actual sandbox state. This module adds a single
``bash`` tool that runs a command in the per-thread sandbox as the non-root
``daytona`` user.

It is NOT a general shell. A default-deny allowlist validator (built on a
``bashlex`` AST walk, modeled on the user's ``bash-deep-check.py`` PreToolUse
hook but with INVERTED polarity — the hook augments a denylist, this builds
an allowlist) permits only read-only inspection programs and rejects
everything else. The validator is a pure function (``validate_bash_command``)
so it can be unit-tested without a sandbox.

Security model: the tool supplements ``run_file``/skills — the only path for
code execution and authenticated data fetches. It passes no credentials, runs
as the non-root ``daytona`` user, rejects every program that can write, build
a command at runtime, or wrap an uninspectable shell body, and fails closed on
any parse error.
"""

import asyncio
import logging
import os

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from .sandbox import SANDBOX_WORKSPACE

logger = logging.getLogger(__name__)

try:
    import bashlex
    import bashlex.errors

    _BASHLEX_IMPORT_ERROR: Exception | None = None
except ImportError as e:  # pragma: no cover - exercised only when extra is absent
    bashlex = None  # type: ignore[assignment]
    _BASHLEX_IMPORT_ERROR = e

# Read-only inspection programs the agent may run. Default-deny: any argv0
# (basename, after stripping leading VAR=value assignments) not in this set
# rejects the whole command. Deliberately excludes every writer/interpreter
# (awk, sed, tee, python, uv, curl, wget, shells) — those run via skills.
ALLOWED_PROGRAMS = {
    "ls",
    "find",
    "stat",
    "file",
    "du",
    "df",
    "wc",
    "tree",
    "realpath",
    "readlink",
    "basename",
    "dirname",
    "cat",
    "head",
    "tail",
    "grep",
    "sort",
    "uniq",
    "cut",
    "tr",
    "xxd",
    "date",
}

# argv0s that build commands at runtime — can't be statically checked, deny.
RUNTIME_ARGV_BUILTINS = {"eval", "source", ".", "exec"}
RUNTIME_ARGV_BINS = {"xargs"}

# argv0s that accept an inline shell command (via -c, -s, heredoc, stdin
# redirect). The body comes from a source we can't statically inspect, so
# every shell-wrapper invocation is rejected outright.
SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}

# find action predicates that execute, delete, or write — reject anywhere in
# a find command's argv (find is otherwise a read-only allowlist member).
FIND_ACTION_PREDICATES = {
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-delete",
    "-fprintf",
    "-fprint",
    "-fprint0",
    "-fls",
}

# Some allowlisted readers carry a flag that writes a file directly, with no
# shell redirect — `sort -o FILE` / `sort -oFILE` / `sort --output=FILE` and
# `tree -o FILE`. Those defeat the read-only model, so the flags are rejected
# even though the program stays on the allowlist. Maps argv0 -> set of write
# flags to reject. Long forms match `--output` and the joined `--output=FILE`;
# the short `-o` form is matched across joined/bundled single-dash tokens in
# `_check_command` (see the rationale comment there). `xxd -r` is NOT a
# file-write flag (it writes the reverse hexdump to stdout), so xxd is
# intentionally absent here.
WRITE_FLAGS_BY_PROGRAM = {
    "sort": {"-o", "--output"},
    "tree": {"-o"},
}

# Short `uniq` flags that consume the FOLLOWING token as a separate numeric
# value (`uniq -f N`, `-s N`, `-w N`). When counting non-flag positionals to
# detect a `uniq INPUT OUTPUT` write, the value token after one of these must
# be skipped so it is not miscounted as a positional. Long `--skip-fields=N`
# etc. are self-contained (`--opt=val`), so they need no skip.
UNIQ_VALUE_SHORT_FLAGS = {"-f", "-s", "-w"}

# Cap on the combined output returned from a single bash call. Inspection
# output (find /, cat of a large file) can be huge; run_file does not cap but
# this tool must, so a single call cannot flood the context window.
MAX_OUTPUT_BYTES = 64 * 1024  # 64 KiB

# Points the agent at the execution/write path when a command is rejected.
_TEACH_SUFFIX = "This is a read-only inspection shell; run code or write files via a skill (run_file)."

# Nested-command constructs the validator rejects WHOLESALE rather than trying
# to validate their inner commands. Three review rounds found RCE/write bypasses
# all rooted in constructs bashlex flattens into uninspectable strings:
# command substitution nested inside parameter expansion (`cat ${x:-$(python
# y)}` — bashlex parses the default-value body as a flat `parameter.value`
# string with no child nodes, so the `$(python y)` inside is never surfaced),
# and command substitution in a here-doc body (handled separately by the
# here-doc redirect rejection). Because we cannot reliably see every inner
# command, we reject the construct itself anywhere it appears — in a word, a
# redirect target, or anywhere else `_walk` descends. Maps a bashlex node kind
# to the teaching message raised when that kind is encountered.
_REJECTED_NODE_KINDS = {
    # `$(...)` and backticks `` `...` `` both parse to `commandsubstitution`.
    "commandsubstitution": (
        "command substitution `$(...)`/backticks are not allowed in the read-only "
        "inspection shell; run code via a skill (run_file), or make a separate "
        "inspection call instead of nesting."
    ),
    # `<(...)` / `>(...)` both parse to `processsubstitution`.
    "processsubstitution": (
        "process substitution `<(...)`/`>(...)` is not allowed in the read-only "
        "inspection shell; use a skill (run_file)."
    ),
    # `$VAR` and `${...}` (including `${x:-...}` default/alternate/assign forms)
    # both parse to `parameter`. Rejecting the whole `parameter` kind closes the
    # `${x:-$(python y)}` bypass where a command substitution hides inside the
    # parameter's flattened, uninspectable value string.
    "parameter": (
        "variable/parameter expansion (`$VAR`, `${...}`) is not allowed in the "
        "read-only inspection shell; use literal paths."
    ),
}


class BashValidationError(Exception):
    """Raised when a command fails the read-only allowlist validation.

    Carries a teaching ``reason`` naming what was rejected and pointing at
    the skill/``run_file`` execution path.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _strip_assignments(argv: list[str]) -> list[str]:
    """Drop leading VAR=value tokens so we see the real argv0."""
    i = 0
    while i < len(argv) and _is_assignment(argv[i]):
        i += 1
    return argv[i:]


def _is_assignment(token: str) -> bool:
    """True if ``token`` is a leading ``NAME=value`` shell assignment."""
    eq = token.find("=")
    if eq <= 0:
        return False
    name = token[:eq]
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in name)


def _basename(s: str) -> str:
    return os.path.basename(s) if "/" in s else s


def _walk(node, found_cmds: list) -> None:
    """Collect (argv, redirects) tuples from a bashlex AST node.

    Descends into the command-LIST constructs — pipelines, ``;``/``&&``/``||``
    lists, compounds, subshell ``( )``, brace group ``{ }``, ``if``/``while``/
    ``for``/``until`` — so EVERY top-level command's argv0 is surfaced for
    allowlist validation. Pipes and globs are command lists / plain words, not
    nested-command constructs, so they keep working.

    It does NOT descend into nested-command constructs (``commandsubstitution``
    for ``$(...)``/backticks, ``processsubstitution`` for ``<(...)``/``>(...)``)
    or into parameter expansion (``parameter`` for ``$VAR``/``${...}``).
    Instead it REJECTS them wholesale by raising ``BashValidationError`` the
    moment one is encountered, no matter where it is nested (in a word, a
    redirect target, a list/compound/subshell child). See
    ``_REJECTED_NODE_KINDS`` for why inner-command validation was abandoned in
    favor of rejecting the construct itself.
    """
    kind = getattr(node, "kind", None)

    # Reject nested-command / parameter-expansion constructs anywhere they
    # appear. Raising here propagates out through ``_collect_commands`` to
    # ``validate_bash_command``, which re-raises ``BashValidationError``.
    rejection = _REJECTED_NODE_KINDS.get(kind)
    if rejection is not None:
        raise BashValidationError(rejection)

    if kind == "command":
        argv: list[str] = []
        redirects: list = []
        for part in node.parts:
            pk = getattr(part, "kind", None)
            if pk in ("word", "assignment"):
                argv.append(getattr(part, "word", ""))
                for sub in getattr(part, "parts", None) or []:
                    _walk(sub, found_cmds)
            elif pk == "redirect":
                redirects.append(part)
                # A redirect node has no `.parts`; its target word lives at
                # `.output` (and `.input`/`.heredoc` for some forms). Walk each
                # so a nested-command construct in a redirect target — e.g.
                # `cat < $(python x)` or a here-string `grep x <<< $(python y)`
                # — is surfaced and REJECTED by the `_REJECTED_NODE_KINDS` check
                # at the top of `_walk` rather than executed.
                for attr in ("output", "input", "heredoc"):
                    child = getattr(part, attr, None)
                    if child is not None:
                        _walk(child, found_cmds)
            else:
                for sub in getattr(part, "parts", None) or []:
                    _walk(sub, found_cmds)
        if argv:
            found_cmds.append((argv, redirects))
        return

    if kind in ("list", "pipeline", "compound", "if", "while", "for", "until"):
        for child in getattr(node, "parts", None) or []:
            _walk(child, found_cmds)
        for child in getattr(node, "list", None) or []:
            _walk(child, found_cmds)
        return

    for child in getattr(node, "parts", None) or []:
        _walk(child, found_cmds)
    for child in getattr(node, "list", None) or []:
        _walk(child, found_cmds)


def _collect_commands(cmdline: str) -> list:
    trees = bashlex.parse(cmdline)
    found: list = []
    for t in trees:
        _walk(t, found)
    return found


# Redirect types that denote OUTPUT — any of these on any command writes a
# file, so reject them (read-only). Plain input ``<`` is allowed.
_OUTPUT_REDIRECT_TYPES = {">", ">>", ">|", "&>", "&>>"}

# Here-document redirect types. A here-doc body is stored as a flat ``.value``
# string on the ``HeredocNode`` with NO ``.parts``, so ``_walk`` never surfaces
# any ``$(...)`` inside it and dash would execute it. The body cannot be
# statically inspected, so any here-doc redirect is rejected (fail closed). The
# here-string ``<<<`` is NOT here: its target is a real word node that ``_walk``
# already descends into and validates.
_HEREDOC_REDIRECT_TYPES = {"<<", "<<-"}


def _redirect_writes_output(redirect) -> bool:
    """True if a bashlex redirect node denotes an output redirect.

    The redirect's ``type`` carries the operator (``>``, ``>>``, ``>|``,
    ``&>``, ``&>>``). Anything containing ``>`` writes; a bare ``<`` (input)
    does not. Matching on the presence of ``>`` also catches fd-qualified
    forms like ``2>``/``1>>`` whose type string still contains ``>``.
    """
    r_type = getattr(redirect, "type", "") or ""
    if r_type in _OUTPUT_REDIRECT_TYPES:
        return True
    return ">" in r_type


def _redirect_is_heredoc(redirect) -> bool:
    """True if a bashlex redirect node is a here-document (``<<`` / ``<<-``)."""
    r_type = getattr(redirect, "type", "") or ""
    return r_type in _HEREDOC_REDIRECT_TYPES


def _check_command(argv: list[str], redirects: list) -> None:
    """Validate a single collected sub-command. Raises BashValidationError.

    Shell wrappers (``bash``/``sh``/…) are rejected outright rather than
    recursed into — their command body comes from a source the validator
    cannot statically inspect — so there is no wrapper-body recursion to
    bound here.
    """
    # Redirect checks run UNCONDITIONALLY, BEFORE the empty-``real`` early
    # return below. An assignment-only command with a redirect (``FOO=bar >
    # out``) strips to an empty ``real``, but ``sh`` still creates/truncates
    # the redirect target — so the redirect must be rejected even when nothing
    # executes. These checks do not depend on argv0.
    for r in redirects:
        # A here-doc body is uninspectable (stored as a flat ``.value`` with no
        # ``.parts``), so any here-doc is rejected — its body could contain a
        # ``$(...)`` the walker never sees. The here-string ``<<<`` is not a
        # here-doc and is still walked/validated as a normal redirect target.
        if _redirect_is_heredoc(r):
            raise BashValidationError(
                "here-documents are not allowed: the body cannot be inspected; this is a "
                "read-only inspection shell — write/run via a skill (run_file)."
            )
        # Output redirection writes a file — reject on any command. Input `<`
        # is allowed (does not write).
        if _redirect_writes_output(r):
            raise BashValidationError(
                f"output redirection is not allowed: this tool cannot write files. {_TEACH_SUFFIX}"
            )

    real = _strip_assignments(argv)
    if not real:
        # Bare assignments (FOO=bar) with no command — nothing executes.
        return

    argv0 = _basename(real[0])

    if argv0 in RUNTIME_ARGV_BUILTINS:
        raise BashValidationError(
            f"`{argv0}` is not allowed: it can build commands at runtime, which cannot be "
            f"statically checked. {_TEACH_SUFFIX}"
        )
    if argv0 in RUNTIME_ARGV_BINS:
        raise BashValidationError(
            f"`{argv0}` is not allowed: it can build commands from stdin, which cannot be "
            f"statically checked. {_TEACH_SUFFIX}"
        )
    if argv0 in SHELL_WRAPPERS:
        raise BashValidationError(
            f"shell wrapper `{argv0}` is not allowed: its command body cannot be statically inspected. {_TEACH_SUFFIX}"
        )

    if argv0 not in ALLOWED_PROGRAMS:
        raise BashValidationError(
            f"`{argv0}` is not allowed in the read-only inspection shell. Allowed programs: "
            f"{', '.join(sorted(ALLOWED_PROGRAMS))}. {_TEACH_SUFFIX}"
        )

    # Per-program write flags (e.g. `sort -o FILE`, `tree -o FILE`) write a
    # file with no shell redirect — reject them even though the program stays
    # on the allowlist.
    write_flags = WRITE_FLAGS_BY_PROGRAM.get(argv0)
    if write_flags:
        for tok in real[1:]:
            # Long form: match `--output` and the joined `--output=FILE`.
            if tok.startswith("--"):
                flag = tok.split("=", 1)[0]
                if flag in write_flags:
                    raise BashValidationError(
                        f"`{argv0} {flag}` writes a file; this is a read-only inspection shell — "
                        f"write via a skill (run_file)."
                    )
                continue
            # Short form: the write flag is `-o` for both `sort` and `tree`,
            # and neither program's other short flags use the letter `o`. So
            # rather than a full getopt parse (unwarranted here), reject any
            # single-dash token whose pre-`=` characters contain `o`. This
            # over-approximation closes every joined/bundled form (`-o`,
            # `-obar`, `-ro`, `-roout`) with only pathological false positives
            # (e.g. `sort -t o` using `o` as a field separator), which fail
            # safe. `--` long flags are handled above; a bare `-` (stdin) has
            # no chars after the dash, so it is unaffected.
            if tok.startswith("-") and "-o" in {f for f in write_flags if len(f) == 2}:
                before_eq = tok.split("=", 1)[0]
                if "o" in before_eq[1:]:
                    raise BashValidationError(
                        f"`{argv0} -o` writes a file; this is a read-only inspection shell — "
                        f"write via a skill (run_file)."
                    )

    # GNU `uniq INPUT OUTPUT` writes OUTPUT via its SECOND positional argument
    # — no flag, so WRITE_FLAGS_BY_PROGRAM cannot catch it. uniq has no
    # two-INPUT form, so 2+ non-flag positionals always means a write. Count
    # the non-flag tokens in real[1:], skipping the numeric value that the
    # short value-taking flags (`-f N`/`-s N`/`-w N`) consume as a SEPARATE
    # arg so it is not miscounted as a positional. Long `--opt=val` forms are
    # self-contained (the value is joined), so they need no skip.
    if argv0 == "uniq":
        positionals = 0
        skip_next = False
        for tok in real[1:]:
            if skip_next:
                # This token is the value of a preceding -f/-s/-w; not a positional.
                skip_next = False
                continue
            if tok in UNIQ_VALUE_SHORT_FLAGS:
                skip_next = True
                continue
            if tok.startswith("-"):
                # Any other flag (including a bare `-` for stdin, and
                # self-contained --opt=val forms) is not a write target.
                continue
            positionals += 1
        if positionals >= 2:
            raise BashValidationError(
                "`uniq`'s second positional argument is an OUTPUT file (it writes); "
                "this is a read-only inspection shell — write via a skill (run_file)."
            )

    # find action predicates execute/delete/write — reject anywhere in argv.
    if argv0 == "find":
        for tok in real[1:]:
            if tok in FIND_ACTION_PREDICATES:
                raise BashValidationError(
                    f"find action predicate `{tok}` is not allowed: it can execute, delete, or "
                    f"write. Use find only to locate files. {_TEACH_SUFFIX}"
                )


def validate_bash_command(cmdline: str) -> None:
    """Validate a command against the read-only inspection allowlist.

    Pure function — no sandbox, no I/O. Parses ``cmdline`` with ``bashlex``,
    walks every top-level command in the command-LIST constructs (pipelines,
    ``;``/``&&``/``||``, subshell ``( )``, brace group ``{ }``, ``if``/
    ``while``/``for``/``until``), and enforces:

      - nested-command and parameter-expansion constructs are REJECTED
        wholesale anywhere they appear — command substitution ``$(...)`` /
        backticks, process substitution ``<(...)``/``>(...)``, and parameter
        expansion ``$VAR``/``${...}`` (see ``_REJECTED_NODE_KINDS``). Inner
        commands of nested constructs are not validated; the construct itself
        is rejected because bashlex flattens some of them (e.g. the default
        value in ``${x:-$(python y)}``) into uninspectable strings. Pipes and
        globs are NOT nested constructs and remain fully allowed;
      - per top-level command, ``argv0`` (basename, after stripping leading
        ``VAR=value`` tokens) must be in ``ALLOWED_PROGRAMS``;
      - no runtime-argv builders (``eval``/``source``/``.``/``exec``/``xargs``)
        or shell wrappers (``bash``/``sh``/…);
      - no output redirection (``>``/``>>``/``>|``/``&>``/``&>>``) and no
        here-documents (``<<``/``<<-``, whose body is uninspectable); these
        redirect checks run even for an assignment-only command
        (``FOO=bar > out`` writes ``out`` though nothing executes). Input
        ``<`` from a plain file is allowed; a nested-command construct in any
        redirect target (e.g. ``cat < $(...)`` or ``grep x <<< $(...)``) is
        rejected by the construct rule;
      - no per-program write flags (``sort -o``/``-obar``/``--output``,
        ``tree -o``);
      - no ``uniq INPUT OUTPUT`` (2+ non-flag positionals — the second is an
        output file uniq writes; value-taking ``-f``/``-s``/``-w`` args are
        not miscounted);
      - no ``find`` action predicates (``-exec``/``-ok``/``-delete``/…).

    Fails closed: a ``bashlex`` parse error, any other parse exception, or
    any internal validator exception → ``BashValidationError`` with a
    teaching message. Never returns normally for an unparseable or unchecked
    command.

    Raises:
        BashValidationError: if the command is rejected.
    """
    if bashlex is None:  # pragma: no cover - exercised only when extra is absent
        raise BashValidationError(
            f"bash inspection is unavailable: the shell parser (bashlex) is not installed "
            f"({_BASHLEX_IMPORT_ERROR}). {_TEACH_SUFFIX}"
        )

    if not cmdline or not cmdline.strip():
        raise BashValidationError(f"empty command. {_TEACH_SUFFIX}")

    try:
        commands = _collect_commands(cmdline)
    except bashlex.errors.ParsingError as e:
        raise BashValidationError(f"could not parse command (rejected to fail closed): {e}. {_TEACH_SUFFIX}") from e
    except BashValidationError:
        raise
    except Exception as e:
        raise BashValidationError(f"command parser error (rejected to fail closed): {e}. {_TEACH_SUFFIX}") from e

    if not commands:
        raise BashValidationError(f"no executable command found (rejected to fail closed). {_TEACH_SUFFIX}")

    for argv, redirects in commands:
        _check_command(argv, redirects)


def _cap_output(text: object) -> str:
    """Truncate combined output to MAX_OUTPUT_BYTES with a clear marker.

    Truncation is on the UTF-8 byte length (the cap is about wire/context
    size, not character count). The marker names the byte limit so the agent
    knows output was cut and can narrow its query.

    Coerces a None/non-str argument to a str defensively so ``.encode()``
    never raises on unexpected sandbox output.
    """
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    truncated = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return (
        truncated + f"\n\n[output truncated to {MAX_OUTPUT_BYTES} bytes — narrow your command "
        "(e.g. add a path, a pattern, or pipe to head) to see more]"
    )


def make_bash():
    """Build the read-only ``bash`` inspection tool.

    Mirrors ``make_run_file``'s factory shape (``@tool`` async fn, ``ToolRuntime``,
    ``Command``/``ToolMessage`` return, ``asyncio.to_thread`` for the blocking
    sandbox call) but needs no ``db`` and injects no credentials. The command
    is validated against the read-only allowlist BEFORE the sandbox is touched;
    a rejected command never reaches the sandbox.

    Not registered in ``_HITL_TOOLS`` — it is read-only and auto-approves.
    """

    @tool
    async def bash(command: str, *, runtime: ToolRuntime) -> Command:
        """Run a read-only inspection command in the sandbox.

        A restricted shell for LOOKING at the sandbox filesystem — listing,
        searching, checking sizes/ages, and peeking at file contents. Runs in
        the per-conversation sandbox as a non-root user, in `/workspace`.
        Pipes, globs, and `;`/`&&`/`||` work and each command is validated.

        Only read-only programs are permitted: ls, find, stat, file, du, df,
        wc, tree, realpath, readlink, basename, dirname, cat, head, tail,
        grep, sort, uniq, cut, tr, xxd, date. It CANNOT write files, run code,
        or fetch data — for any of those, run a skill via `run_file`. Output
        redirection, interpreters (python, awk, sed), shell wrappers, command
        substitution (`$(...)`/backticks), process substitution (`<(...)`),
        and variable expansion (`$VAR`/`${...}`) are rejected — use literal
        paths and run separate inspection calls instead of nesting.

        Args:
            command: The shell command to run. Validated against the
                read-only allowlist; a rejected command is not executed.
        """
        try:
            validate_bash_command(command)
        except BashValidationError as e:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"bash rejected: {e.reason}",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                    ]
                }
            )

        from .sandbox import _get_or_create_sandbox, exec_as_daytona, is_sandbox_available

        if not is_sandbox_available():
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content="Error: Code sandbox is not available (DAYTONA_API_KEY not set).",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )

        thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")

        def _run():
            sandbox = _get_or_create_sandbox(thread_id)
            # Runs as the non-root daytona user (su -l wrap) in /workspace.
            # No credentials are injected — env= is omitted entirely.
            response = exec_as_daytona(sandbox, command, cwd=SANDBOX_WORKSPACE)
            # The sandbox SDK may return None or a non-str result; coerce to a
            # str so _cap_output's .encode() never raises on the output.
            output = response.result
            if output is None:
                output = ""
            elif not isinstance(output, str):
                output = str(output)
            if response.exit_code != 0:
                return f"Error (exit code {response.exit_code}):\n{output}"
            return output

        try:
            result = await asyncio.to_thread(_run)
        except Exception as e:
            # Sandbox create/exec can raise (Daytona down, quota, network).
            # Return a concise error ToolMessage rather than propagating — the
            # tool must always return a Command. Don't leak exception internals
            # beyond the type name.
            logger.exception("bash sandbox interaction failed")
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"Error: bash sandbox interaction failed ({type(e).__name__}).",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                    ],
                }
            )

        result = _cap_output(result)

        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=result,
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    return bash
