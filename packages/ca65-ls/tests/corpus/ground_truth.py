"""Source-derived ground truth for the corpus contract suite.

Everything here is deliberately naive text scanning, independent of
tree-sitter, so that it can act as an oracle for what the parser *should*
find. It only claims the unambiguous cases; anything it cannot decide (macro
bodies, conditional assembly) it leaves out rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Directives whose operand is a single new symbol name.
_BLOCK_DIRECTIVES = {
    "proc": "proc",
    "scope": "scope",
    "macro": "macro",
    "mac": "macro",
    "struct": "struct",
    "union": "union",
    "enum": "enum",
}

_DIRECTIVE_RE = re.compile(
    r"^\s*\.(?P<directive>proc|scope|macro|mac|struct|union|enum)\s+(?P<name>[A-Za-z_@][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_CALL_RE = re.compile(
    r"^\s*(?:[A-Za-z_@][A-Za-z0-9_]*:\s*)?(?P<op>jsr|jmp)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b(?!\s*::)",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r";.*$")
_MACRO_START = re.compile(r"^\s*\.(macro|mac)\b", re.IGNORECASE)
_MACRO_END = re.compile(r"^\s*\.(endmacro|endmac)\b", re.IGNORECASE)
_STRUCT_START = re.compile(r"^\s*\.(struct|union|enum)\b", re.IGNORECASE)
_STRUCT_END = re.compile(r"^\s*\.(endstruct|endunion|endenum)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Declaration:
    name: str
    kind: str
    line: int  # 0-based


@dataclass(frozen=True)
class CallSite:
    name: str
    line: int  # 0-based
    column: int  # 0-based column of the operand


def _strip_comment(line: str) -> str:
    # Good enough: no string literal in this corpus contains ';' before a
    # directive of interest, and we only look at line starts anyway.
    return _COMMENT_RE.sub("", line)


def block_declarations(path: Path) -> list[Declaration]:
    """`.proc NAME`-style declarations at file level (outside macro bodies)."""
    out: list[Declaration] = []
    in_macro = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = _strip_comment(raw)
        if _MACRO_END.search(line):
            in_macro = max(0, in_macro - 1)
            continue
        m = _DIRECTIVE_RE.match(line)
        if m:
            kind = _BLOCK_DIRECTIVES[m.group("directive").lower()]
            if in_macro == 0 or kind == "macro":
                out.append(Declaration(m.group("name"), kind, lineno))
        if _MACRO_START.search(line):
            in_macro += 1
    return out


def call_sites(path: Path) -> list[CallSite]:
    """`jsr NAME` / `jmp NAME` operands outside macro bodies, unqualified names only."""
    out: list[CallSite] = []
    in_macro = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = _strip_comment(raw)
        if _MACRO_END.search(line):
            in_macro = max(0, in_macro - 1)
            continue
        if _MACRO_START.search(line):
            in_macro += 1
            continue
        if in_macro:
            continue
        m = _CALL_RE.match(line)
        if m:
            out.append(CallSite(m.group("name"), lineno, m.start("name")))
    return out


_ACME_RE = re.compile(
    r"^\s*!(?:zone|byte|word|text|pet|scr|to|cpu|source|macro|if|ifdef|fill|align|convtab)\b",
    re.IGNORECASE | re.MULTILINE,
)


def is_acme(path: Path) -> bool:
    """ACME-dialect source: `!zone`, `!byte`, ... A .asm extension says nothing
    about the dialect; c64-nist-curves keeps ACME and CA65 twins side by side
    and c64-sid-instruments is ACME throughout."""
    try:
        return bool(_ACME_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False
