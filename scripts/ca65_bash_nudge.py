#!/usr/bin/env python3
"""
ca65_bash_nudge -- PreToolUse hook nudging CA65 work off `grep`+`sed` and onto
Serena's symbolic tools.

Why this exists
---------------
Serena ships its own reminder hook, but on Claude Code it is blind to `Bash`:
``PreToolUseRemindAboutSymbolicToolsHook`` matches only the native ``Read`` and
``Grep`` tool *names* (``serena/hooks.py`` ``is_read_call`` / ``is_grep_call``),
while the ``GROK`` and ``CODEX`` branches additionally classify shell commands.
Real CA65 sessions read source almost entirely through Bash -- one measured
session ran 203 Bash calls and a single ``Read`` -- so its counter never
advances and the nudge never fires.

Extending Serena's hook instead would fire machine-wide across the ~40 suffixes
in its ``_CODE_FILE_EXTENSIONS`` and add a commit to carry through every rebase.
This stays scoped to assembly files inside projects that actually have the ca65
backend enabled, so no other language is affected.

What counts
-----------
The project is the nearest ancestor of the payload ``cwd`` holding a
``.serena/project.yml`` (block or flow style, comments and BOM tolerated); it
must list ``ca65`` under ``language_servers:`` (or the older ``languages:``).
Outside such a project the hook does nothing.

The command is parsed like a shell would: heredoc bodies are dropped, quotes
are respected, and the text is split on newlines and on ``;``, ``&&``, ``||``,
``|``, ``&``, ``( )``, ``$( )`` and backticks. Each simple command is
classified by its first token after leading keywords (``do``, ``then``, ``if``,
``while``, ``time``, ``{`` ...), ``VAR=value`` assignments and wrappers
(``env``, ``nice``, ``timeout N``, ``command``, ``sudo``, ``xargs`` ...).
``cd`` is tracked so later relative paths resolve against the right directory,
and ``for VAR in WORDS`` / ``VAR=value`` bindings are substituted into ``$VAR``.

A call counts when one of its simple commands is

* a **read** -- ``cat``, ``head``, ``tail``, ``sed`` (not in-place), ``less``,
  ``more``, ``bat``, ``nl``, ``awk``, ``diff``, ``git show REV:path`` -- with an
  assembly-file operand (``.s``/``.inc``/``.asm``/``.mac``, any case, globs and
  ``{a,b}.s`` accepted, ``main.s.bak`` rejected) that resolves inside the
  project; or
* a **grep** -- ``grep``/``rg``/``ag``/``ack``/``git grep`` -- with such an
  operand, or a recursive search whose root directory (explicit or the current
  directory) lies inside the project and contains assembly files; or
* the same via ``xargs`` / ``find -exec``, where the producer stage (``find src
  -name '*.s'``, ``ls src/*.s``) supplies the evidence.

A stage that only consumes a pipe (``ca65 x.s | head``) is not a read. Paths
outside the project (absolute, ``../``, after ``cd elsewhere``) never count,
because Serena's tools only see the active project, and neither does a path
the hook cannot place (``cat $UNKNOWN``). Deliberately uncounted: ``python
-c``, ``bash -c``, ``eval``, ``od``, ``strings``, ``cut``, ``wc``. Deliberately
still counted: ``grep -q/-c/-l`` and reads whose output is redirected.

Behaviour
---------
Counts consecutive qualifying Bash calls per session and emits a ``deny`` on
the third, naming the symbolic tool that would have answered the query. The
deny resets the count, so the verbatim retry proceeds. For the next
``_DENY_INTERVAL_SECONDS`` after a deny the hook keeps counting but stays
quiet; once the window closes, the next qualifying call is denied if the count
has reached the threshold again. Any symbolic Serena tool call (through any
``mcp__serena*__`` server name) resets the count; a gap of
``_RESET_AFTER_SECONDS`` starts a fresh burst.

State lives in ``~/.claude/ca65-bash-nudge/<session_id>.json``; a payload
without a session id is ignored entirely. Invalid state is rewritten rather
than trusted, and files older than a day are pruned opportunistically.

Reads the Claude Code PreToolUse payload on stdin and always exits 0; any
unexpected failure is swallowed, since a hook must never block real work.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sys
import time

STATE_DIR = pathlib.Path.home() / ".claude" / "ca65-bash-nudge"

_THRESHOLD = 3
_DENY_INTERVAL_SECONDS = 120
#: A burst is only a burst while the calls keep coming; a long gap starts over.
_RESET_AFTER_SECONDS = 600
#: State files untouched for this long belong to dead sessions.
_STATE_MAX_AGE_SECONDS = 24 * 3600
_PRUNE_EVERY_SECONDS = 3600

#: Everything the classifier does is linear in the command length, but a cap
#: keeps a pathological single-line blob from costing more than a few ms.
_MAX_SCAN_CHARS = 32_768
#: Bound on how many operands, brace expansions and directory entries we look
#: at, so a hostile command cannot turn the hook into a filesystem walk.
_MAX_OPERANDS = 64
_MAX_BRACE_EXPANSIONS = 32
_MAX_DIR_VISITS = 400

ASM_SUFFIXES = frozenset((".s", ".inc", ".asm", ".mac"))
#: rg/ag type names that mean assembly.
_ASM_TYPE_NAMES = frozenset(("asm", "s"))

_READ_COMMANDS = frozenset(
    ("cat", "head", "tail", "sed", "less", "more", "bat", "nl", "awk", "diff")
)
_GREP_COMMANDS = frozenset(("grep", "rg", "ag", "ack", "fgrep", "egrep"))
#: Recursive by default: any directory operand (or the cwd) is a search root.
_RECURSIVE_GREPS = frozenset(("rg", "ag", "ack", "git-grep"))

#: Options taking a separate argument, per command, so their values are not
#: mistaken for operands.
_OPTS_WITH_ARG: dict[str, frozenset[str]] = {
    "grep": frozenset(
        (
            "-e",
            "-f",
            "-m",
            "-A",
            "-B",
            "-C",
            "-d",
            "-D",
            "--include",
            "--exclude",
            "--exclude-dir",
            "--regexp",
            "--file",
        )
    ),
    "git-grep": frozenset(
        (
            "-e",
            "-f",
            "-m",
            "-A",
            "-B",
            "-C",
            "-O",
            "--open-files-in-pager",
            "--max-depth",
            "--threads",
        )
    ),
    "rg": frozenset(
        (
            "-e",
            "-f",
            "-g",
            "-t",
            "-T",
            "-m",
            "-A",
            "-B",
            "-C",
            "-M",
            "-E",
            "-j",
            "-r",
            "--type",
            "--type-not",
            "--glob",
            "--iglob",
            "--regexp",
            "--file",
            "--max-count",
            "--context",
            "--replace",
            "--encoding",
            "--max-depth",
            "--threads",
            "--color",
            "--sort",
            "--sortr",
            "--pre",
        )
    ),
    "ag": frozenset(
        (
            "-A",
            "-B",
            "-C",
            "-G",
            "-m",
            "-p",
            "--file-search-regex",
            "--ignore",
            "--ignore-dir",
            "--pager",
            "--depth",
        )
    ),
    "ack": frozenset(
        ("-A", "-B", "-C", "-m", "-G", "--type", "--ignore-dir", "--ignore-file", "--pager")
    ),
    "head": frozenset(("-n", "-c")),
    "tail": frozenset(("-n", "-c", "-s", "--pid")),
    "sed": frozenset(("-e", "-f", "-l", "--expression", "--file", "--line-length")),
    "awk": frozenset(("-f", "-F", "-v")),
    "nl": frozenset(("-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w")),
    "diff": frozenset(
        (
            "-I",
            "-D",
            "-W",
            "-x",
            "-X",
            "-S",
            "-F",
            "-C",
            "-U",
            "--exclude",
            "--exclude-from",
            "--starting-file",
        )
    ),
    "bat": frozenset(
        ("-l", "-r", "-H", "-m", "--language", "--line-range", "--style", "--theme", "--map-syntax")
    ),
    "less": frozenset(
        ("-b", "-h", "-j", "-k", "-o", "-O", "-p", "-P", "-t", "-T", "-x", "-y", "-z", "-#")
    ),
}
#: Where the first bare operand is a pattern/program rather than a path,
#: unless one of these options supplied it instead.
_FIRST_OPERAND_IS_PATTERN: dict[str, frozenset[str]] = {
    "grep": frozenset(("-e", "-f", "--regexp", "--file")),
    "git-grep": frozenset(("-e", "-f")),
    "rg": frozenset(("-e", "-f", "--regexp", "--file", "--files", "--type-list")),
    "ag": frozenset(("-G", "--file-search-regex", "-l")),
    "ack": frozenset(("--match", "-f", "-g")),
    "sed": frozenset(("-e", "-f", "--expression", "--file")),
    "awk": frozenset(("-f",)),
}

_KEYWORDS = frozenset(
    (
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "in",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
        "!",
        "{",
        "}",
    )
)
#: Wrapper command -> (options taking an argument, positional args to skip).
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "time": (frozenset(), 0),
    "env": (frozenset(("-u", "-C", "-S", "--unset", "--chdir")), 0),
    "nice": (frozenset(("-n", "--adjustment")), 0),
    "nohup": (frozenset(), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset(("-a",)), 0),
    "sudo": (frozenset(("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t")), 0),
    "doas": (frozenset(("-u", "-C")), 0),
    "timeout": (frozenset(("-k", "-s", "--kill-after", "--signal")), 1),
    "stdbuf": (frozenset(("-i", "-o", "-e")), 0),
    "unbuffer": (frozenset(), 0),
    "caffeinate": (frozenset(("-t", "-w")), 0),
    "xargs": (
        frozenset(
            (
                "-I",
                "-n",
                "-P",
                "-L",
                "-s",
                "-d",
                "-E",
                "-a",
                "-i",
                "--max-args",
                "--max-procs",
                "--max-lines",
                "--replace",
                "--delimiter",
                "--arg-file",
            )
        ),
        0,
    ),
}
_GIT_OPTS_WITH_ARG = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace"))

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_BRACE_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_GLOB_CHARS = frozenset("*?[")

#: Serena tools that answer a structural question; any of them clears the burst.
_SYMBOLIC_TOOLS = frozenset(
    (
        "find_symbol",
        "get_symbols_overview",
        "find_referencing_symbols",
        "find_declaration",
        "find_implementations",
        "rename_symbol",
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
    )
)
#: The MCP server may be registered under any name starting with ``serena``.
_SERENA_TOOL_RE = re.compile(r"^mcp__serena[A-Za-z0-9-]*__([A-Za-z0-9_]+)$")


# --------------------------------------------------------------------------- lexing


_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?!<)-?[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z_][A-Za-z0-9_.-]*))"
)


def _strip_heredocs(text: str) -> str:
    """Drop heredoc bodies; the ``<<TAG`` on the command line itself stays."""
    pos = 0
    while True:
        m = _HEREDOC_RE.search(text, pos)
        if m is None:
            return text
        tag = m.group(1) or m.group(2) or m.group(3) or ""
        pos = m.end()
        body_start = text.find("\n", pos)
        if body_start < 0:
            return text
        body_start += 1
        term = re.compile(r"^\t*" + re.escape(tag) + r"[ \t]*\r?$", re.M).search(text, body_start)
        body_end = len(text) if term is None else min(len(text), term.end() + 1)
        text = text[:body_start] + text[body_end:]


#: One linear pass; every alternative is unambiguous so there is no backtracking.
_SCAN_RE = re.compile(
    r"""
    (?P<ws>[ \t\r]+|\\\n)
  | (?P<nl>\n)
  | (?P<op>&&|\|\||\|&|;;|<<<|<<-|<<|>>|>&|<&|&>|>\||\$\(|[;|&<>()`])
  | (?P<sq>'[^']*')
  | (?P<dq>"(?:[^"\\$]|\\.|\$\([^()"]*\)|\$)*")
  | (?P<esc>\\.)
  | (?P<word>[^\s'"\\;|&<>()`$\#]+|\$|\#)
  | (?P<other>.)
    """,
    re.X | re.S,
)
_DQ_UNESCAPE_RE = re.compile(r'\\([\\"$`])')
#: A simple ``$(...)`` inside double quotes; its body is classified separately.
_EMBEDDED_RE = re.compile(r"\$\(([^()\"]*)\)")

_OP = 1
_WORD = 0


def _tokenize(text: str, embedded: list[str] | None = None) -> list[tuple[int, str]]:
    """Shell-ish lexer: returns ``(kind, text)`` with kind ``_OP`` or ``_WORD``.

    Quotes are removed from words, ``\\x`` yields ``x``, ``#`` at the start of a
    word begins a comment that runs to the end of the line. Command
    substitutions inside double quotes are appended to ``embedded``.
    """
    tokens: list[tuple[int, str]] = []
    word: list[str] = []
    in_comment = False

    def flush() -> None:
        if word:
            tokens.append((_WORD, "".join(word)))
            word.clear()

    for m in _SCAN_RE.finditer(text):
        kind = m.lastgroup
        if in_comment:
            if kind == "nl":
                in_comment = False
                tokens.append((_OP, "\n"))
            continue
        if kind == "ws":
            flush()
        elif kind == "nl":
            flush()
            tokens.append((_OP, "\n"))
        elif kind == "op":
            flush()
            tokens.append((_OP, m.group()))
        elif kind == "sq":
            word.append(m.group()[1:-1])
        elif kind == "dq":
            inner = m.group()[1:-1]
            if embedded is not None and "$(" in inner:
                embedded.extend(_EMBEDDED_RE.findall(inner))
            word.append(_DQ_UNESCAPE_RE.sub(r"\1", inner))
        elif kind == "esc":
            word.append(m.group()[1])
        elif kind == "word":
            piece = m.group()
            if piece == "#" and not word:
                in_comment = True
                continue
            word.append(piece)
        else:
            word.append(m.group())
    flush()
    return tokens


_SEPARATORS = frozenset(("&&", "||", "|&", ";;", ";", "|", "&", "\n", "(", ")", "$(", "`"))
_PIPES = frozenset(("|", "|&"))
_DROP_NEXT = frozenset((">", ">>", ">&", "<&", "&>", ">|", "<<", "<<-", "<<<"))
_READ_NEXT = "<"


class _Context:
    """What the classifier knows about where the command runs."""

    __slots__ = ("root", "cwd", "home", "bindings", "dir_cache", "cwd_stack", "in_backtick")

    def __init__(self, root: str, cwd: str, home: str) -> None:
        self.root = root
        self.cwd: str | None = cwd
        self.home = home
        self.bindings: dict[str, list[str]] = {}
        self.dir_cache: dict[str, bool] = {}
        self.cwd_stack: list[str | None] = []
        self.in_backtick = False


def _simple_commands(
    tokens: list[tuple[int, str]],
) -> list[tuple[list[str], bool, list[str] | None, str]]:
    """Group tokens into simple commands.

    Yields ``(words, consumes_pipe, producer_words, opening_op)`` where
    ``producer_words`` is the previous pipeline stage when ``consumes_pipe``.
    Redirections are folded away: ``>`` targets are dropped, ``<`` sources are
    kept as operands, heredoc tags are dropped.
    """
    out: list[tuple[list[str], bool, list[str] | None, str]] = []
    words: list[str] = []
    consumes_pipe = False
    producer: list[str] | None = None
    opening = ""
    skip_next = False
    i = 0
    n = len(tokens)
    while i < n:
        kind, text = tokens[i]
        i += 1
        if kind == _WORD:
            if skip_next:
                skip_next = False
            else:
                words.append(text)
            continue
        skip_next = False
        if text in _SEPARATORS:
            out.append((words, consumes_pipe, producer, opening))
            consumes_pipe = text in _PIPES
            producer = words if consumes_pipe else None
            words = []
            opening = text
            continue
        # A redirection. A bare fd number right before it is not an operand.
        if words and words[-1].isdigit():
            words.pop()
        if text in _DROP_NEXT:
            skip_next = True
        # ``<`` keeps its target: ``cat < main.s`` reads main.s.
    out.append((words, consumes_pipe, producer, opening))
    return out


# --------------------------------------------------------------------------- paths


def _expand_braces(word: str) -> list[str]:
    results = [word]
    for _ in range(8):
        nxt: list[str] = []
        changed = False
        for w in results:
            m = _BRACE_RE.search(w)
            if m is None:
                nxt.append(w)
                continue
            changed = True
            for alt in m.group(1).split(","):
                nxt.append(w[: m.start()] + alt + w[m.end() :])
                if len(nxt) > _MAX_BRACE_EXPANSIONS:
                    return nxt[:_MAX_BRACE_EXPANSIONS]
        results = nxt
        if not changed:
            break
    return results


def _substitute(word: str, ctx: _Context) -> list[str]:
    """Apply known ``for``/assignment bindings; a lone ``$VAR`` may expand to several words."""
    if "$" not in word:
        return [word]
    m = _VAR_RE.fullmatch(word)
    if m is not None:
        name = m.group(1) or m.group(2)
        if name in ctx.bindings:
            return list(ctx.bindings[name])
        return [word]

    def repl(mm: re.Match[str]) -> str:
        name = mm.group(1) or mm.group(2)
        values = ctx.bindings.get(name)
        return values[0] if values else mm.group(0)

    return [_VAR_RE.sub(repl, word)]


def _operand_forms(word: str, ctx: _Context) -> list[str]:
    """All concrete spellings of an operand, or none if it cannot be resolved."""
    forms: list[str] = []
    for sub in _substitute(word, ctx):
        if "$" in sub or "`" in sub or "{}" in sub:
            continue  # unresolved variable or xargs placeholder
        forms.extend(_expand_braces(sub))
    return forms[:_MAX_BRACE_EXPANSIONS]


def _is_asm_name(path: str) -> bool:
    base = os.path.basename(path)
    if not base or base.startswith("."):
        return False
    return os.path.splitext(base)[1].lower() in ASM_SUFFIXES


def _resolve(path: str, ctx: _Context) -> str | None:
    if path.startswith("~"):
        path = ctx.home + path[1:] if path == "~" or path.startswith("~/") else path
    if os.path.isabs(path):
        return os.path.realpath(path)
    if ctx.cwd is None:
        return None
    return os.path.normpath(os.path.join(ctx.cwd, path))


def _inside_project(path: str, ctx: _Context) -> bool:
    return path == ctx.root or path.startswith(ctx.root + os.sep)


def _dir_has_asm(path: str, ctx: _Context) -> bool:
    cached = ctx.dir_cache.get(path)
    if cached is not None:
        return cached
    found = False
    stack = [path]
    visits = 0
    while stack and visits < _MAX_DIR_VISITS and not found:
        visits += 1
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if name not in ("node_modules", "build", "dist", "venv", "__pycache__"):
                                stack.append(entry.path)
                        elif os.path.splitext(name)[1].lower() in ASM_SUFFIXES:
                            found = True
                            break
                    except OSError:
                        continue
        except OSError:
            continue
    ctx.dir_cache[path] = found
    return found


def _asm_file_evidence(word: str, ctx: _Context) -> bool:
    """Is ``word`` an assembly file (or glob of them) inside the project?"""
    for form in _operand_forms(word, ctx):
        if not _is_asm_name(form):
            continue
        resolved = _resolve(form, ctx)
        if resolved is not None and _inside_project(resolved, ctx):
            return True
    return False


def _asm_dir_evidence(word: str, ctx: _Context) -> bool:
    """Is ``word`` an existing directory inside the project holding assembly?"""
    for form in _operand_forms(word, ctx):
        if any(c in _GLOB_CHARS for c in form):
            continue
        resolved = _resolve(form, ctx)
        if resolved is None or not _inside_project(resolved, ctx):
            continue
        try:
            if not os.path.isdir(resolved):
                continue
        except OSError:
            continue
        if _dir_has_asm(resolved, ctx):
            return True
    return False


def _cwd_has_asm(ctx: _Context) -> bool:
    return ctx.cwd is not None and _inside_project(ctx.cwd, ctx) and _dir_has_asm(ctx.cwd, ctx)


# --------------------------------------------------------------------------- classification


def _split_options(cmd: str, args: list[str]) -> tuple[list[str], list[str]]:
    """Separate ``args`` into options (with their values) and bare operands."""
    with_arg = _OPTS_WITH_ARG.get(cmd, frozenset())
    options: list[str] = []
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            operands.extend(args[i:])
            break
        if a.startswith("-") and a != "-":
            options.append(a)
            if a in with_arg and i < len(args):
                options.append(args[i])
                i += 1
        else:
            operands.append(a)
    return options, operands[:_MAX_OPERANDS]


def _sed_in_place(options: list[str]) -> bool:
    for o in options:
        if o.startswith("--in-place"):
            return True
        if re.match(r"^-[A-Za-z]*i", o):
            return True
    return False


def _grep_is_recursive(cmd: str, options: list[str]) -> bool:
    if cmd in _RECURSIVE_GREPS:
        return True
    for o in options:
        if o in ("--recursive", "--dereference-recursive"):
            return True
        if o.startswith("-d") and "recurse" in o:
            return True
        if re.match(r"^-[A-Za-z]*[rR]", o):
            return True
    return False


def _grep_filters(cmd: str, options: list[str]) -> tuple[bool, bool]:
    """(any file filter given, any of them selects assembly)."""
    any_filter = False
    asm_filter = False
    i = 0
    while i < len(options):
        o = options[i]
        i += 1
        value: str | None = None
        if o.startswith(("--include=", "--glob=", "--iglob=", "--type=", "-g=", "-t=")):
            value = o.split("=", 1)[1]
        elif o in ("--include", "-g", "--glob", "--iglob", "-t", "--type") and i < len(options):
            value = options[i]
            i += 1
        elif o.startswith(("-g", "-t")) and len(o) > 2 and cmd == "rg":
            value = o[2:]
        if value is None:
            continue
        any_filter = True
        if value.lower() in _ASM_TYPE_NAMES or _is_asm_name(value):
            asm_filter = True
    return any_filter, asm_filter


def _classify_grep(
    cmd: str, args: list[str], consumes_pipe: bool, ctx: _Context, piped_asm: bool
) -> bool:
    if piped_asm:
        return True
    options, operands = _split_options(cmd, args)
    pattern_opts = _FIRST_OPERAND_IS_PATTERN.get(cmd, frozenset())
    if operands and not any(
        o in pattern_opts or o.split("=", 1)[0] in pattern_opts for o in options
    ):
        operands = operands[1:]
    if any(_asm_file_evidence(w, ctx) for w in operands):
        return True
    if not _grep_is_recursive(cmd, options):
        return False
    any_filter, asm_filter = _grep_filters(cmd, options)
    if any_filter and not asm_filter:
        return False
    roots = [w for w in operands if _asm_dir_evidence(w, ctx)]
    if roots:
        return True
    if operands or consumes_pipe:
        return False
    return _cwd_has_asm(ctx)


def _classify_read(cmd: str, args: list[str], ctx: _Context, piped_asm: bool) -> bool:
    options, operands = _split_options(cmd, args)
    if cmd == "sed" and _sed_in_place(options):
        return False
    if piped_asm:
        return True
    pattern_opts = _FIRST_OPERAND_IS_PATTERN.get(cmd)
    if (
        pattern_opts is not None
        and operands
        and not any(o in pattern_opts or o.split("=", 1)[0] in pattern_opts for o in options)
    ):
        operands = operands[1:]
    return any(_asm_file_evidence(w, ctx) for w in operands)


def _producer_evidence(words: list[str] | None, ctx: _Context) -> bool:
    """Would the pipe producer (``find``, ``ls``, ``git ls-files``) enumerate project assembly?"""
    if not words:
        return False
    name = os.path.basename(words[0]).lower()
    if name == "find":
        return _find_evidence(words[1:], ctx)
    operands = [w for w in words[1:] if not w.startswith("-")][:_MAX_OPERANDS]
    return any(_asm_file_evidence(w, ctx) or _asm_dir_evidence(w, ctx) for w in operands)


def _find_evidence(args: list[str], ctx: _Context) -> bool:
    """Would this ``find`` enumerate assembly files inside the project?"""
    roots: list[str] = []
    filters: list[str] = []
    i = 0
    while i < len(args) and not args[i].startswith(("-", "(", "!")):
        roots.append(args[i])
        i += 1
    while i < len(args):
        a = args[i]
        i += 1
        if a in (
            "-name",
            "-iname",
            "-path",
            "-ipath",
            "-wholename",
            "-regex",
            "-iregex",
        ) and i < len(args):
            filters.append(args[i])
            i += 1
        elif a in ("-exec", "-execdir", "-ok", "-okdir"):
            break
    if filters and not any(_is_asm_name(f) for f in filters):
        return False
    return any(_asm_dir_evidence(r, ctx) for r in roots or ["."])


def _find_exec_command(args: list[str]) -> list[str] | None:
    for i, a in enumerate(args):
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            return args[i + 1 :]
    return None


def _skip_wrapper(words: list[str], i: int, with_arg: frozenset[str], positional: int) -> int:
    i += 1
    while i < len(words):
        w = words[i]
        if w == "--":
            i += 1
            break
        if _ASSIGN_RE.match(w):
            i += 1
            continue
        if w.startswith("-") and w != "-":
            i += 2 if w in with_arg else 1
            continue
        break
    return min(len(words), i + positional)


def _bind_for(words: list[str], ctx: _Context) -> None:
    # for VAR in WORD...
    if len(words) >= 3 and words[1] not in _KEYWORDS and words[2] == "in":
        values: list[str] = []
        for w in words[3:]:
            values.extend(_substitute(w, ctx))
        ctx.bindings[words[1]] = values[:_MAX_OPERANDS]


def _apply_cd(args: list[str], ctx: _Context) -> None:
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    if not operands:
        ctx.cwd = ctx.home
        return
    target = operands[0]
    if target == "-":
        ctx.cwd = None
        return
    forms = _operand_forms(target, ctx)
    if len(forms) != 1 or any(c in _GLOB_CHARS for c in forms[0]):
        ctx.cwd = None
        return
    ctx.cwd = _resolve(forms[0], ctx)


def _classify_simple(
    words: list[str], consumes_pipe: bool, producer: list[str] | None, ctx: _Context
) -> str | None:
    """Return 'read', 'grep' or None for one simple command; tracks cd and bindings."""
    i = 0
    piped_asm = False
    while i < len(words):
        w = words[i]
        if w in _KEYWORDS:
            if w == "for":
                _bind_for(words[i:], ctx)
                return None
            i += 1
            continue
        if _ASSIGN_RE.match(w):
            name, _, value = w.partition("=")
            ctx.bindings[name] = _substitute(value, ctx) if value else [""]
            i += 1
            continue
        name = os.path.basename(w).lower()
        if name in _WRAPPERS:
            if name == "xargs":
                piped_asm = _producer_evidence(producer, ctx)
                consumes_pipe = False
            with_arg, positional = _WRAPPERS[name]
            i = _skip_wrapper(words, i, with_arg, positional)
            continue
        break
    if i >= len(words):
        return None
    cmd = os.path.basename(words[i]).lower()
    args = words[i + 1 :]

    if cmd in ("cd", "pushd"):
        _apply_cd(args, ctx)
        return None
    if cmd == "git":
        return _classify_git(args, consumes_pipe, ctx)
    if cmd == "find":
        sub = _find_exec_command(args)
        if sub:
            sub_cmd = os.path.basename(sub[0]).lower()
            kind = (
                "grep"
                if sub_cmd in _GREP_COMMANDS
                else "read"
                if sub_cmd in _READ_COMMANDS
                else None
            )
            if kind is not None and _find_evidence(args, ctx):
                return kind
        return None
    if cmd in _GREP_COMMANDS:
        return "grep" if _classify_grep(cmd, args, consumes_pipe, ctx, piped_asm) else None
    if cmd in _READ_COMMANDS:
        return "read" if _classify_read(cmd, args, ctx, piped_asm) else None
    return None


def _classify_git(args: list[str], consumes_pipe: bool, ctx: _Context) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_OPTS_WITH_ARG else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1 :]
    if sub == "grep":
        return "grep" if _classify_grep("git-grep", rest, consumes_pipe, ctx, False) else None
    if sub == "show":
        for a in rest:
            if a.startswith("-"):
                continue
            rev, colon, path = a.partition(":")
            if colon and path and _asm_file_evidence(path, ctx):
                return "read"
        return None
    return None


def _command_kind(command: str, ctx: _Context) -> str | None:
    """Return 'read', 'grep', or None for the whole (possibly compound) command."""
    text = _strip_heredocs(command)[:_MAX_SCAN_CHARS]
    kind: str | None = None
    embedded: list[str] = []
    tokens = _tokenize(text, embedded)
    for snippet in embedded[:16]:
        tokens.append((_OP, "\n"))
        tokens.extend(_tokenize(snippet))
    for words, consumes_pipe, producer, opening in _simple_commands(tokens):
        if opening == "`":
            ctx.in_backtick = not ctx.in_backtick
            opening = "(" if ctx.in_backtick else ")"
        if opening in ("(", "$("):
            ctx.cwd_stack.append(ctx.cwd)
        elif opening == ")" and ctx.cwd_stack:
            ctx.cwd = ctx.cwd_stack.pop()
        if not words:
            continue
        found = _classify_simple(words, consumes_pipe, producer, ctx)
        if found == "grep":
            return "grep"
        if found == "read":
            kind = "read"
    return kind


# --------------------------------------------------------------------------- project


def _parse_language_servers(text: str) -> set[str]:
    """Languages enabled in a Serena project.yml; language_servers wins over languages."""
    found: dict[str, set[str]] = {}
    current: str | None = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        indented = line[0] in " \t"
        stripped = line.strip()
        if current is not None and stripped.startswith("-"):
            found[current].add(_yaml_item(stripped[1:]))
            continue
        if not indented:
            current = None
            key, sep, value = stripped.partition(":")
            if sep and key in ("languages", "language_servers"):
                current = key
                found.setdefault(key, set())
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    found[key].update(_yaml_item(v) for v in value[1:-1].split(","))
                elif value:
                    found[key].add(_yaml_item(value))
                continue
    if "language_servers" in found:
        return found["language_servers"]
    return found.get("languages", set())


def _yaml_item(value: str) -> str:
    return value.strip().strip("\"'").strip().lower()


def _find_project(cwd: str) -> str | None:
    """Nearest ancestor of cwd with a .serena/project.yml; its path if ca65 is enabled."""
    if not cwd or not os.path.isabs(cwd):
        return None
    current = os.path.realpath(cwd)
    for _ in range(64):
        config = os.path.join(current, ".serena", "project.yml")
        try:
            with open(config, encoding="utf-8", errors="replace") as fh:
                text = fh.read(65536)
        except OSError:
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
            continue
        return current if "ca65" in _parse_language_servers(text) else None
    return None


# --------------------------------------------------------------------------- state


def _state_path(session_id: str) -> pathlib.Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:96]
    return STATE_DIR / f"{safe}.json"


def _valid_time(value: object, now: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= now + 60
    )


def _load(path: pathlib.Path, now: float) -> dict:
    """Read the session state; anything that does not look right becomes a fresh state."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    count = raw.get("count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return {}
    state = {"count": count}
    for key in ("last_seen", "last_deny"):
        if key in raw:
            if not _valid_time(raw[key], now):
                return {}
            state[key] = raw[key]
    return state


def _save(path: pathlib.Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError as exc:
        print(f"ca65_bash_nudge: cannot write {path}: {exc}", file=sys.stderr)


def _prune_state(now: float) -> None:
    """Drop state files of long-dead sessions, at most once an hour."""
    marker = STATE_DIR / ".last-prune"
    try:
        if now - marker.stat().st_mtime < _PRUNE_EVERY_SECONDS:
            return
    except OSError:
        pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with os.scandir(STATE_DIR) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if now - entry.stat().st_mtime > _STATE_MAX_AGE_SECONDS:
                        os.unlink(entry.path)
                except OSError:
                    continue
        marker.touch()
    except OSError:
        pass


def _deny(kind: str) -> None:
    if kind == "grep":
        suggestion = (
            "`find_referencing_symbols` finds every use site of a name, and "
            "`get_symbols_overview` lists what a file defines"
        )
    else:
        suggestion = (
            "`find_symbol` with `include_body=True` returns a routine's body "
            "directly, without locating it by line number first"
        )
    reason = (
        f"Several assembly files read via Bash in a row. This project has the ca65 "
        f"language server enabled: {suggestion}. Reach for the symbolic tools here. "
        f"The counter is reset, so retry this command now if it is still what you want."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool = payload.get("tool_name")
    session = payload.get("session_id")
    if not isinstance(tool, str) or not isinstance(session, str) or not session:
        return 0
    path = _state_path(session)
    now = time.time()

    # Any symbolic tool call means the agent is already doing the right thing.
    m = _SERENA_TOOL_RE.match(tool)
    if m is not None:
        if m.group(1) in _SYMBOLIC_TOOLS:
            state = _load(path, now)
            state["count"] = 0
            _save(path, state)
        return 0

    if tool != "Bash":
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return 0

    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        return 0
    root = _find_project(cwd)
    if root is None:
        return 0

    ctx = _Context(root, os.path.realpath(cwd), str(pathlib.Path.home()))
    kind = _command_kind(command, ctx)
    if kind is None:
        return 0

    _prune_state(now)
    state = _load(path, now)
    if now - state.get("last_seen", 0) > _RESET_AFTER_SECONDS:
        state["count"] = 0
    count = state.get("count", 0) + 1
    state["last_seen"] = now
    in_quiet_window = now - state.get("last_deny", 0) < _DENY_INTERVAL_SECONDS

    if count >= _THRESHOLD and not in_quiet_window:
        state["count"] = 0
        state["last_deny"] = now
        _save(path, state)
        _deny(kind)
        return 0

    state["count"] = count
    _save(path, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - a hook must never block real work.
        print(f"ca65_bash_nudge: {exc!r}", file=sys.stderr)
        sys.exit(0)
