"""
Shared data types — the contract between the buffer layer, the project index,
and the LSP server. Pinned early so the Parser engineer (buffer/) and the
Indexer engineer (index/) can build in parallel against a stable shape.

Conventions
-----------
- Positions and ranges use **0-based** lines and characters (LSP convention).
- A `BufferSymbol` describes what the buffer parser found in a single document.
- A `WorkspaceSymbol` is the project-index record: same as BufferSymbol plus
  the source URI and any enrichments from cc65 .dbg debug info.
- Both are immutable (frozen dataclasses) so they can be cached freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SymbolKind(str, Enum):
    """The CA65-flavored symbol kinds the buffer layer extracts.

    These map onto LSP `SymbolKind` values in the server layer; kept distinct
    here so the parser doesn't have to know about LSP enums.
    """

    PROC = "proc"          # .proc Name ... .endproc
    SCOPE = "scope"        # .scope Name ... .endscope
    MACRO = "macro"        # .macro Name ... .endmacro
    STRUCT = "struct"      # .struct Name ... .endstruct
    UNION = "union"        # .union Name ... .endunion
    ENUM = "enum"          # .enum Name ... .endenum
    LABEL = "label"        # plain top-level / in-scope label
    CHEAP_LOCAL = "cheap_local"   # @foo: scoped to enclosing non-cheap label
    ANON_LABEL = "anon_label"     # : (unnamed; addressed by :+ / :-)
    CONSTANT = "constant"  # name = expr   or   name := expr
    SEGMENT = "segment"    # .segment "NAME"
    IMPORT = "import"      # .import / .importzp
    EXPORT = "export"      # .export / .exportzp (the declarator, not the target)
    FIELD = "field"        # member of a .struct / .union


@dataclass(frozen=True)
class Position:
    """0-based (line, character) — matches LSP."""

    line: int
    character: int


@dataclass(frozen=True)
class Range:
    """LSP-style half-open range. `end` is exclusive."""

    start: Position
    end: Position


@dataclass(frozen=True)
class BufferSymbol:
    """A symbol found by parsing a single buffer / document.

    No cross-file resolution — that's the Indexer's job. This is purely what
    you can extract from one file's syntax tree.
    """

    name: str
    kind: SymbolKind
    range: Range                       # the entire span (e.g. .proc..endproc)
    selection_range: Range             # just the name identifier (for goto-def)
    scope_path: tuple[str, ...]        # ("helpers", "foo") for helpers::foo
    parent_label: Optional[str]        # for CHEAP_LOCAL: enclosing non-cheap label
    children: tuple["BufferSymbol", ...] = ()    # for nested scopes/procs/structs


@dataclass(frozen=True)
class WorkspaceSymbol:
    """A symbol in the project-wide index, including its source URI and any
    enrichment from cc65 .dbg debug info (address, segment).
    """

    name: str
    kind: SymbolKind
    uri: str                           # file:///... of the defining document
    range: Range
    selection_range: Range
    scope_path: tuple[str, ...]
    parent_label: Optional[str]
    # Enrichment from .dbg (populated when a build's debug info is available):
    address: Optional[int] = None
    segment: Optional[str] = None      # CODE / BSS / ZEROPAGE / ...
    size: Optional[int] = None


@dataclass(frozen=True)
class SymbolReference:
    """A use-site of a symbol. The Indexer collects these per-file from the
    buffer layer and aggregates them workspace-wide so `textDocument/references`
    can answer in O(1) lookup.
    """

    name: str
    uri: str
    range: Range
    scope_path: tuple[str, ...]        # scope in which the reference appears,
                                       # used to disambiguate same-named locals
