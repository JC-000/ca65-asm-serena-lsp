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

Behaviour
---------
Counts consecutive qualifying Bash calls per session and emits a ``deny`` on the
third, naming the symbolic tool that would have answered the query. A deny
resets the counter, so the very next retry proceeds; any symbolic tool call also
resets it. At most one nudge per ``_DENY_INTERVAL_SECONDS``.

Reads the Claude Code PreToolUse payload on stdin and always exits 0; any
unexpected failure is swallowed, since a hook must never block real work.
"""

from __future__ import annotations

import json
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

ASM_SUFFIXES = ("s", "inc", "asm", "mac")
#: Matches an assembly path anywhere in the command, including glob forms such
#: as ``--include=*.s``. The trailing boundary keeps ``.s`` from matching inside
#: an unrelated word like ``foo.style``.
_ASM_RE = re.compile(r"[\w*./~-]+\.(?:" + "|".join(ASM_SUFFIXES) + r")(?![\w])")

#: Shell commands that read or search file content.
_READ_COMMANDS = frozenset(
    ("cat", "head", "tail", "sed", "less", "more", "bat", "nl", "awk")
)
_GREP_COMMANDS = frozenset(("grep", "rg", "ag", "ack", "fgrep", "egrep"))

#: Serena tools that answer a structural question; any of them clears the burst.
_SYMBOLIC_TOOLS = (
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


def _segments(command: str) -> list[str]:
    """Split a compound command into its parts.

    Serena's hook reads only the first token, so ``cd src && grep foo bar.s``
    registers as ``cd`` and is missed. Splitting on the shell's sequencing
    operators catches those.
    """
    return [seg.strip() for seg in re.split(r"&&|\|\||[;|]", command) if seg.strip()]


def _command_kind(command: str) -> str | None:
    """Return 'read', 'grep', or None for the whole (possibly compound) command."""
    kind: str | None = None
    for segment in _segments(command):
        tokens = segment.split()
        if not tokens:
            continue
        name = os.path.basename(tokens[0]).lower()
        # `sed -i` rewrites the file; that is an edit, not a read.
        if name == "sed" and any(t.startswith("-i") for t in tokens[1:]):
            continue
        if name in _GREP_COMMANDS:
            return "grep"
        if name in _READ_COMMANDS:
            kind = "read"
    return kind


def _is_ca65_project(cwd: str) -> bool:
    """Is the ca65 backend enabled for the project we are working in?

    Reads the project's own Serena config rather than a baked list, so a project
    that gains or loses the backend is picked up without touching this hook.
    """
    if not cwd:
        return False
    config = pathlib.Path(cwd) / ".serena" / "project.yml"
    try:
        text = config.read_text(errors="replace")
    except OSError:
        return False
    in_list = False
    for line in text.splitlines():
        if line.startswith(("languages:", "language_servers:")):
            in_list = True
            continue
        if in_list:
            stripped = line.strip()
            if stripped.startswith("-"):
                if stripped.lstrip("- ").strip().strip("\"'") == "ca65":
                    return True
            elif stripped and not line.startswith((" ", "\t")):
                in_list = False
    return False


def _state_path(session_id: str) -> pathlib.Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "default")[:96]
    return STATE_DIR / f"{safe}.json"


def _load(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: pathlib.Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
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
                    "additionalContext": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0

    tool = payload.get("tool_name") or ""
    session = payload.get("session_id") or ""
    path = _state_path(session)

    # Any symbolic tool call means the agent is already doing the right thing.
    if tool.startswith("mcp__serena__") and any(t in tool for t in _SYMBOLIC_TOOLS):
        state = _load(path)
        state["count"] = 0
        _save(path, state)
        return 0

    if tool != "Bash":
        return 0

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command:
        return 0

    kind = _command_kind(command)
    if kind is None or not _ASM_RE.search(command):
        return 0
    if not _is_ca65_project(payload.get("cwd") or ""):
        return 0

    now = time.time()
    state = _load(path)

    # Stay quiet for a while after a nudge rather than denying repeatedly.
    if now - float(state.get("last_deny", 0)) < _DENY_INTERVAL_SECONDS:
        return 0
    if now - float(state.get("last_seen", 0)) > _RESET_AFTER_SECONDS:
        state["count"] = 0

    count = int(state.get("count", 0)) + 1
    state["last_seen"] = now

    if count >= _THRESHOLD:
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
    except Exception:
        # A hook must never block real work.
        sys.exit(0)
