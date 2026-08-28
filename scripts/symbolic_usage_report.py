#!/usr/bin/env python3
"""
symbolic_usage_report -- measure how often CA65 work uses Serena's symbolic
tools versus raw reads/edits.

Motivation
----------
Serena's web dashboard (`/get_tool_stats`) counts only Serena MCP calls and
resets every time the MCP process restarts, so it structurally cannot answer
"are the ca65 symbolic tools actually being used?".  Claude Code's own
transcripts under ``~/.claude/projects/<slug>/*.jsonl`` record *every* tool
call -- native Read/Grep/Edit included -- with a timestamp and cwd, so they
give the complete picture and can be replayed retroactively.

This reports, for CA65 projects only, the share of assembly-file operations
that went through a symbolic tool rather than a raw read/grep/edit.

Usage
-----
    scripts/symbolic_usage_report.py                       # last 24h
    scripts/symbolic_usage_report.py --since 2026-08-01    # explicit window
    scripts/symbolic_usage_report.py --since ... --until ...
    scripts/symbolic_usage_report.py --json                # machine-readable
    scripts/symbolic_usage_report.py --append PATH         # append JSONL snapshot
    scripts/symbolic_usage_report.py --projects PATH       # explicit project scope

Exit code is always 0; this is a reporting tool.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys

TRANSCRIPT_ROOT = pathlib.Path.home() / ".claude" / "projects"
RIG_CONFIG_ROOT = (
    pathlib.Path.home()
    / "Documents"
    / "new-computer-setup"
    / "claude-code-rig"
    / "state"
    / "serena-config"
)
ASM_SUFFIXES = (".s", ".inc", ".asm", ".mac")

#: Serena tools that answer a question about *structure*.  These are the ones
#: the ca65 language server backs; usage of these is what we want to go up.
SYMBOLIC_TOOLS = {
    "find_symbol",
    "get_symbols_overview",
    "find_referencing_symbols",
    "find_declaration",
    "find_implementations",
    "rename_symbol",
    "safe_delete_symbol",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "get_diagnostics_for_file",
    "get_diagnostics_for_symbol",
}

#: Serena tools that treat a file as flat text.
SERENA_TEXT_TOOLS = {
    "read_file",
    "search_for_pattern",
    "replace_content",
    "replace_in_files",
    "create_text_file",
    "delete_lines",
    "replace_lines",
    "insert_at_line",
}

NATIVE_READ_TOOLS = {"Read", "Grep", "Glob"}
NATIVE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

#: Shell commands that are really a file read in disguise.
_READ_SHELL = ("cat ", "head ", "tail ", "sed -n", "grep ", "rg ", "less ", "bat ")


def _parse_when(value: str) -> dt.datetime:
    """Accept a date, a full ISO timestamp, or a `<N>h` / `<N>d` offset."""
    value = value.strip()
    if value.endswith(("h", "d")) and value[:-1].replace(".", "").isdigit():
        n = float(value[:-1])
        delta = dt.timedelta(hours=n) if value.endswith("h") else dt.timedelta(days=n)
        return _now() - delta
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ca65_projects() -> set[str]:
    """Project directory names whose Serena config enables the ca65 backend.

    Falls back to an empty set if the rig config tree is absent; the caller
    then treats every project with assembly files as in scope.
    """
    found: set[str] = set()
    if not RIG_CONFIG_ROOT.is_dir():
        return found
    for cfg in RIG_CONFIG_ROOT.glob("*/project.yml"):
        try:
            text = cfg.read_text(errors="replace")
        except OSError:
            continue
        in_list = False
        for line in text.splitlines():
            if line.startswith(("languages:", "language_servers:")):
                in_list = True
                continue
            if in_list:
                stripped = line.strip()
                if stripped.startswith("-"):
                    if stripped.lstrip("- ").strip().strip("\"'") == "ca65":
                        found.add(cfg.parent.name)
                elif stripped and not line.startswith((" ", "\t")):
                    in_list = False
    return found


def _targets_asm(tool: str, tool_input: dict) -> bool:
    """Does this call operate on an assembly file?

    Symbolic tools may legitimately omit a path (a workspace-wide
    `find_symbol`); inside a CA65 project those still count, since the ca65
    backend is what answers them.
    """
    if not isinstance(tool_input, dict):
        return False
    for key in ("file_path", "relative_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.lower().endswith(ASM_SUFFIXES):
            return True
    for key in ("pattern", "glob", "query", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and any(s in value.lower() for s in ASM_SUFFIXES):
            return True
    return False


def _classify(tool: str, tool_input: dict) -> str | None:
    """Return a bucket name, or None if the call is irrelevant to the metric."""
    if tool.startswith("mcp__serena__"):
        bare = tool[len("mcp__serena__") :]
        if bare in SYMBOLIC_TOOLS:
            return "symbolic"
        if bare in SERENA_TEXT_TOOLS:
            return "serena_text" if _targets_asm(tool, tool_input) else None
        return None
    if tool in NATIVE_READ_TOOLS:
        return "native_read" if _targets_asm(tool, tool_input) else None
    if tool in NATIVE_EDIT_TOOLS:
        return "native_edit" if _targets_asm(tool, tool_input) else None
    if tool == "Bash":
        command = (tool_input or {}).get("command", "")
        if isinstance(command, str) and any(command.lstrip().startswith(c) for c in _READ_SHELL):
            return "shell_read" if _targets_asm(tool, tool_input) else None
    return None


def collect(
    since: dt.datetime, until: dt.datetime, projects: set[str] | None = None
) -> dict:
    in_scope = projects if projects is not None else ca65_projects()
    per_project: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    tool_detail: collections.Counter = collections.Counter()
    sessions: set[str] = set()

    for transcript in TRANSCRIPT_ROOT.glob("*/*.jsonl"):
        # Cheap pre-filter: skip files untouched during the window.
        try:
            if dt.datetime.fromtimestamp(transcript.stat().st_mtime, dt.timezone.utc) < since:
                continue
        except OSError:
            continue
        try:
            handle = transcript.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                stamp = entry.get("timestamp")
                if not isinstance(stamp, str):
                    continue
                try:
                    when = _parse_when(stamp)
                except ValueError:
                    continue
                if not (since <= when <= until):
                    continue
                cwd = entry.get("cwd") or ""
                project = pathlib.PurePath(cwd).name if cwd else transcript.parent.name
                if in_scope and project not in in_scope:
                    continue
                message = entry.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool = block.get("name") or ""
                    bucket = _classify(tool, block.get("input") or {})
                    if bucket is None:
                        continue
                    per_project[project][bucket] += 1
                    tool_detail[tool] += 1
                    sessions.add(str(transcript))

    totals: collections.Counter = collections.Counter()
    for counts in per_project.values():
        totals.update(counts)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "generated": _now().isoformat(),
        "sessions": len(sessions),
        "projects": {name: dict(counts) for name, counts in sorted(per_project.items())},
        "totals": dict(totals),
        "by_tool": dict(tool_detail.most_common()),
    }


def _share(counts: dict) -> tuple[int, int, float | None]:
    symbolic = counts.get("symbolic", 0)
    raw = (
        counts.get("native_read", 0)
        + counts.get("native_edit", 0)
        + counts.get("serena_text", 0)
        + counts.get("shell_read", 0)
    )
    total = symbolic + raw
    return symbolic, raw, (symbolic / total if total else None)


def render(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"# CA65 symbolic-tool usage: {data['since'][:16]} -> {data['until'][:16]}")
    lines.append("")
    symbolic, raw, share = _share(data["totals"])
    if share is None:
        lines.append("No assembly-file tool activity in this window.")
        return "\n".join(lines)
    lines.append(f"**{share:.1%} symbolic** ({symbolic} symbolic vs {raw} raw) "
                 f"across {data['sessions']} session(s)")
    lines.append("")
    lines.append("| project | symbolic | raw | share |")
    lines.append("|---|---:|---:|---:|")
    for name, counts in data["projects"].items():
        p_sym, p_raw, p_share = _share(counts)
        if p_sym + p_raw == 0:
            continue
        lines.append(f"| {name} | {p_sym} | {p_raw} | {p_share:.1%} |")
    lines.append("")
    lines.append("| tool | calls |")
    lines.append("|---|---:|")
    for tool, count in data["by_tool"].items():
        lines.append(f"| `{tool}` | {count} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="24h", help="ISO timestamp/date, or an offset like 24h / 7d (default: 24h)")
    parser.add_argument("--until", default=None, help="ISO timestamp/date (default: now)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--append", metavar="PATH", help="append a JSON snapshot as one line to PATH")
    parser.add_argument(
        "--projects",
        metavar="PATH",
        help="JSON file holding the list of in-scope project names. Use when the rig "
        "config under ~/Documents is unreadable -- a launchd job is denied that path "
        "by TCC, so the scheduled collector reads a pre-baked list instead.",
    )
    args = parser.parse_args(argv)

    scope: set[str] | None = None
    if args.projects:
        scope = set(json.loads(pathlib.Path(args.projects).expanduser().read_text()))

    since = _parse_when(args.since)
    until = _parse_when(args.until) if args.until else _now()
    data = collect(since, until, scope)

    if args.append:
        target = pathlib.Path(args.append).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(json.dumps(data) + "\n")

    print(json.dumps(data, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
