"""
Buffer-layer Document: parses a single CA65 source file with tree-sitter
and produces a list of :class:`BufferSymbol` records the Indexer can consume.

Grammar: ``pogyomo/tree-sitter-ca65`` pinned at commit ``b22ead1``, vendored
under ``vendor/tree-sitter-ca65/`` and compiled into this package as
``ca65_ls._grammar`` (see the NOTICE.md there for provenance and update
procedure); it exposes the same ``language()`` factory as the upstream
Python package, which the ``tree_sitter`` runtime wraps in a :class:`Language`.

See ``docs/research/ts-ca65-coverage.md`` for the grammar coverage report.
The relevant grammar gaps we cope with here are:

* **Gap #4** (macro/label ambiguity at statement position) — anything that
  parses as ``macro_inst`` whose name does *not* match a ``.macro``
  definition in the same file is downgraded to a plain label reference.
  Macro invocations are never emitted as symbols.
* **Gap #1** (leading ``::name`` global-scope operator) — currently lands
  as an ``ERROR`` node; we don't emit it as a definition. References are
  recovered by token-walk in :meth:`Document.references_in`.
* The remaining gaps (65C02/65816 mnemonics, anonymous ``.enum``, block
  comments) do not affect our synthetic corpus and are noted only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from tree_sitter import Language, Node, Parser, Tree

from ca65_ls import _grammar as _ts_ca65

from ..types import BufferSymbol, Position, Range, SymbolKind, SymbolReference

_log = logging.getLogger(__name__)


# --- single shared Language / Parser ------------------------------------- #

_LANGUAGE: Optional[Language] = None


def _get_language() -> Language:
    global _LANGUAGE
    if _LANGUAGE is None:
        _LANGUAGE = Language(_ts_ca65.language())
    return _LANGUAGE


def _new_parser() -> Parser:
    return Parser(_get_language())


# --- internal helpers ---------------------------------------------------- #


def _pt(node_point) -> Position:
    """tree-sitter Point → our :class:`Position`."""
    return Position(line=node_point[0], character=node_point[1])


def _node_range(node: Node) -> Range:
    return Range(start=_pt(node.start_point), end=_pt(node.end_point))


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_child_of_type(node: Node, type_name: str) -> Optional[Node]:
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_descendant_symbol(node: Node) -> Optional[Node]:
    """Return the first ``symbol`` descendant — used for nodes whose
    grammar shape is ``wrapper(symbol)`` (e.g. ``pseudo_inst_proc_symbol``).
    """
    for c in node.children:
        if c.type == "symbol":
            return c
    # fall through to a recursive search (cheap — these wrappers are shallow)
    for c in node.children:
        found = _find_descendant_symbol(c)
        if found is not None:
            return found
    return None


# Statement-shape nodes the grammar emits inside a ``pseudo_inst_block``.
# Used to walk into bodies (procs, scopes, macros, etc.).
_BLOCK_TYPE = "pseudo_inst_block"


# --- the Document --------------------------------------------------------- #


@dataclass
class _CollectState:
    """Mutable state threaded through the recursive collector."""

    src: bytes
    macro_defs: set[str]
    # Stack of current scope path components: ``(name, is_cheap_anchor)``.
    # ``is_cheap_anchor`` is True when the enclosing context anchors cheap
    # locals (``.proc``, ``.scope``, or a plain label).
    scope_stack: list[tuple[str, bool]]
    # Current enclosing non-cheap label name (for ``parent_label`` on
    # cheap locals).  None at top level.
    parent_label: Optional[str]


class Document:
    """A parsed CA65 source document.

    Public API matches the contract in the M2 plan:

    * ``symbols`` — hierarchical top-level :class:`BufferSymbol` records.
    * ``flat_symbols()`` — flattened recursive view.
    * ``references_in(name)`` — every occurrence of ``name`` outside of
      a binding position.
    * ``update(new_text)`` — incremental re-parse with tree-sitter's
      ``edit`` + ``parse(old_tree=...)`` API.
    """

    __slots__ = ("uri", "_text", "_parser", "_tree", "_symbols", "_flat", "_all_refs")

    def __init__(self, uri: str, text: str) -> None:
        self.uri = uri
        self._text = text
        self._parser = _new_parser()
        self._tree: Tree = self._parser.parse(text.encode("utf-8"))
        self._symbols: tuple[BufferSymbol, ...] = ()
        self._flat: tuple[BufferSymbol, ...] = ()
        self._all_refs: tuple[SymbolReference, ...] | None = None
        self._extract()

    # ------------------------------------------------------------------ #
    # public                                                              #
    # ------------------------------------------------------------------ #

    @property
    def text(self) -> str:
        return self._text

    @property
    def tree(self) -> Tree:
        return self._tree

    @property
    def symbols(self) -> list[BufferSymbol]:
        """Hierarchical, top-level only."""
        return list(self._symbols)

    def flat_symbols(self) -> list[BufferSymbol]:
        """Every symbol, including nested children, in document order."""
        return list(self._flat)

    def references_in(self, name: str) -> list[SymbolReference]:
        """Return every textual occurrence of ``name`` that is *not* the
        defining position of a symbol of the same name.

        Definitions of macros/procs/scopes/structs/etc. are skipped via a
        set of (start_byte, end_byte) pairs collected during extraction.
        Anonymous-label references (``:+``/``:-``) are out of scope here;
        the Indexer resolves those numerically against anonymous-label
        positions stored in the BufferSymbol list.

        For collecting references to MANY names at once (e.g. indexing a
        whole workspace), prefer :meth:`all_references` — it walks the
        tree once instead of once per name, which is materially faster.
        """
        if not name:
            return []
        return [r for r in self.all_references() if r.name == name]

    def all_references(self) -> list[SymbolReference]:
        """Single-pass collection of every identifier reference in the file.

        Returns one :class:`SymbolReference` per identifier-shaped token at
        a non-defining position.  Cheaper than calling :meth:`references_in`
        once per name: O(parse_tree) instead of O(parse_tree × names).
        The Indexer uses this for cold reindex.

        Cached after the first call; invalidated by :meth:`update`.
        """
        if self._all_refs is not None:
            return list(self._all_refs)
        src = self._text.encode("utf-8")
        defn_spans = self._definition_spans()
        out: list[SymbolReference] = []
        self._walk_all_references(self._tree.root_node, src, defn_spans, out, scope=())
        self._all_refs = tuple(out)
        return list(out)

    def update(self, new_text: str) -> None:
        """Re-parse with incremental help from the old tree.

        We don't have edit-deltas in this minimal API, so we apply a
        whole-buffer edit and let tree-sitter's incremental parser
        decide what to reuse.
        """
        old_src = self._text.encode("utf-8")
        new_src = new_text.encode("utf-8")

        # Tell the existing tree the whole buffer changed.
        if self._tree is not None and old_src != new_src:
            old_lines = self._text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            old_end_row = max(len(old_lines) - 1, 0)
            old_end_col = len(old_lines[-1]) if old_lines else 0
            new_end_row = max(len(new_lines) - 1, 0)
            new_end_col = len(new_lines[-1]) if new_lines else 0
            self._tree.edit(
                start_byte=0,
                old_end_byte=len(old_src),
                new_end_byte=len(new_src),
                start_point=(0, 0),
                old_end_point=(old_end_row, old_end_col),
                new_end_point=(new_end_row, new_end_col),
            )

        self._text = new_text
        self._tree = self._parser.parse(new_src, self._tree)
        self._symbols = ()
        self._flat = ()
        self._all_refs = None
        self._extract()

    # ------------------------------------------------------------------ #
    # extraction                                                          #
    # ------------------------------------------------------------------ #

    def _extract(self) -> None:
        src = self._text.encode("utf-8")
        root = self._tree.root_node
        self._log_errors(root)

        # First pass: collect macro definition names (file-local). Needed to
        # disambiguate macro_inst vs label-reference per Gap #4.
        macro_defs: set[str] = set()
        self._collect_macro_defs(root, src, macro_defs)

        state = _CollectState(
            src=src,
            macro_defs=macro_defs,
            scope_stack=[],
            parent_label=None,
        )
        top: list[BufferSymbol] = []
        self._walk_collect(root, state, top)

        # Post-process: extend each plain LABEL's body range until the next
        # sibling boundary, and absorb cheap-locals / anonymous-labels that
        # appear inside that body as children.  This makes ``label:``-style
        # routines work in find_symbol(include_body=True) and renders nested
        # outlines for code that doesn't use ``.proc``.
        src_lines = self._text.splitlines()
        top = _synthesize_label_bodies(top, src_lines)

        self._symbols = tuple(top)
        self._flat = tuple(self._flatten(top))

    @staticmethod
    def _flatten(syms: list[BufferSymbol]) -> list[BufferSymbol]:
        out: list[BufferSymbol] = []
        for s in syms:
            out.append(s)
            if s.children:
                out.extend(Document._flatten(list(s.children)))
        return out

    def _log_errors(self, root: Node) -> None:
        """Walk the tree once and log ERROR nodes. We emit best-effort
        symbols regardless — this is purely diagnostic for now.
        """
        # Cheap iterative walk; only log the first handful so a wildly
        # broken file doesn't drown the logger.
        max_log = 5
        count = 0
        cursor = root.walk()
        try:
            visited = False
            while True:
                if not visited and (cursor.node.is_error or cursor.node.type == "ERROR"):
                    if count < max_log:
                        _log.debug(
                            "parse error at %s in %s",
                            cursor.node.start_point,
                            self.uri,
                        )
                    count += 1
                if not visited and cursor.goto_first_child():
                    continue
                if cursor.goto_next_sibling():
                    visited = False
                    continue
                if not cursor.goto_parent():
                    break
                visited = True
        finally:
            del cursor

    def _collect_macro_defs(self, node: Node, src: bytes, out: set[str]) -> None:
        if node.type == "pseudo_inst_macro":
            name_node = _first_child_of_type(node, "symbol")
            if name_node is not None:
                out.add(_text(name_node, src))
        for c in node.children:
            self._collect_macro_defs(c, src, out)

    # ------------------------------------------------------------------ #
    # recursive symbol collector                                          #
    # ------------------------------------------------------------------ #

    def _walk_collect(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        """Recursively descend the parse tree, appending top-level
        symbols of the *current* scope to ``out``. Nested-scope symbols
        end up as ``children`` of their enclosing container.
        """
        node_type = node.type

        # --- container-like nodes that introduce children ----------------- #
        if node_type == "pseudo_inst_proc":
            self._handle_proc_like(node, state, out, kind=SymbolKind.PROC)
            return
        if node_type == "pseudo_inst_scope":
            self._handle_scope(node, state, out)
            return
        if node_type == "pseudo_inst_macro":
            self._handle_macro(node, state, out)
            return
        if node_type == "pseudo_inst_struct":
            self._handle_struct_or_union(node, state, out, kind=SymbolKind.STRUCT)
            return
        if node_type == "pseudo_inst_union":
            self._handle_struct_or_union(node, state, out, kind=SymbolKind.UNION)
            return
        if node_type == "pseudo_inst_enum":
            self._handle_enum(node, state, out)
            return

        # --- single-statement nodes ----------------------------------- #
        if node_type == "label":
            self._handle_plain_label(node, state, out)
            return
        if node_type == "local_label":
            self._handle_cheap_local(node, state, out)
            return
        if node_type == "unnamed_label":
            self._handle_anon_label(node, state, out)
            return
        if node_type == "pseudo_inst_segment":
            self._handle_segment(node, state, out)
            return
        if node_type == "symbol_eq" or node_type == "symbol_assign":
            self._handle_constant(node, state, out)
            return
        if node_type in (
            "pseudo_inst_import",
            "pseudo_inst_importzp",
        ):
            self._handle_import_export(node, state, out, kind=SymbolKind.IMPORT,
                                       symbol_child_type=("pseudo_inst_import_symbol"
                                                          if node_type == "pseudo_inst_import"
                                                          else "pseudo_inst_importzp_symbol"))
            return
        if node_type in (
            "pseudo_inst_export",
            "pseudo_inst_exportzp",
        ):
            self._handle_import_export(node, state, out, kind=SymbolKind.EXPORT,
                                       symbol_child_type=("pseudo_inst_export_symbol"
                                                          if node_type == "pseudo_inst_export"
                                                          else "pseudo_inst_exportzp_symbol"))
            return
        if node_type in ("pseudo_inst_global", "pseudo_inst_globalzp"):
            self._handle_global(node, state, out)
            return

        # macro_inst at statement position: NOT a symbol (per Gap #4 we
        # only emit definitions). Skip its body; nothing inside is a
        # binding.
        if node_type == "macro_inst":
            return

        # Generic descent for everything else (source, source_line,
        # pseudo_inst_block, conditional bodies, etc.).
        for c in node.children:
            self._walk_collect(c, state, out)

    # ---- container handlers ----------------------------------------- #

    def _handle_proc_like(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
        *,
        kind: SymbolKind,
    ) -> None:
        # pseudo_inst_proc has a pseudo_inst_proc_symbol child whose
        # symbol descendant is the name.
        name_wrapper = _first_child_of_type(node, "pseudo_inst_proc_symbol")
        name_node = _find_descendant_symbol(name_wrapper) if name_wrapper else None
        if name_node is None:
            return
        name = _text(name_node, state.src)
        children: list[BufferSymbol] = []
        # New scope frame; proc is a cheap-local anchor.
        prev_parent = state.parent_label
        state.scope_stack.append((name, True))
        state.parent_label = name
        try:
            block = _first_child_of_type(node, _BLOCK_TYPE)
            if block is not None:
                for c in block.children:
                    self._walk_collect(c, state, children)
        finally:
            state.scope_stack.pop()
            state.parent_label = prev_parent

        out.append(
            BufferSymbol(
                name=name,
                kind=kind,
                range=_node_range(node),
                selection_range=_node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=tuple(children),
            )
        )

    def _handle_scope(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # Name is optional. pseudo_inst_scope_symbol wraps it.
        name_wrapper = _first_child_of_type(node, "pseudo_inst_scope_symbol")
        name_node = _find_descendant_symbol(name_wrapper) if name_wrapper else None
        name = _text(name_node, state.src) if name_node is not None else ""

        children: list[BufferSymbol] = []
        prev_parent = state.parent_label
        if name:
            state.scope_stack.append((name, True))
            # Scopes don't change the cheap-local anchor unless they're named;
            # but cheap locals inside a .scope without an enclosing .proc are
            # anchored to nothing (top-of-scope). Reset parent_label so cheap
            # locals show up with parent_label=None until a label or proc
            # appears.
            state.parent_label = None
        try:
            block = _first_child_of_type(node, _BLOCK_TYPE)
            if block is not None:
                for c in block.children:
                    self._walk_collect(c, state, children)
        finally:
            if name:
                state.scope_stack.pop()
                state.parent_label = prev_parent

        if not name:
            # Anonymous scope: do not emit a SCOPE symbol; just bubble its
            # children up to the caller's scope.
            out.extend(children)
            return

        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.SCOPE,
                range=_node_range(node),
                selection_range=_node_range(name_node) if name_node else _node_range(node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=tuple(children),
            )
        )

    def _handle_macro(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # First ``symbol`` child of pseudo_inst_macro is the macro name;
        # any subsequent ``symbol`` children before the block are params.
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            return
        name = _text(name_node, state.src)
        # Macros don't form a CA65 scope in the symbol-table sense (the
        # body is essentially expanded textually), but for outline
        # purposes we still want to attach any nested labels/locals so
        # the indexer can see them. Walk the body; don't push a scope
        # frame.
        children: list[BufferSymbol] = []
        block = _first_child_of_type(node, _BLOCK_TYPE)
        if block is not None:
            # Macro params: skip the macro_inst body collection; we just
            # need labels inside. Reuse current state.
            for c in block.children:
                self._walk_collect(c, state, children)

        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.MACRO,
                range=_node_range(node),
                selection_range=_node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=tuple(children),
            )
        )

    def _handle_struct_or_union(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
        *,
        kind: SymbolKind,
    ) -> None:
        # ``.struct Name`` / ``.union Name`` — first ``symbol`` child is the
        # type name; ``pseudo_inst_struct_or_union_field`` children carry
        # the fields.
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            return
        name = _text(name_node, state.src)

        children: list[BufferSymbol] = []
        scope_path_inside = self._current_scope_path(state) + (name,)
        for c in node.children:
            if c.type == "pseudo_inst_struct_or_union_field":
                field_name_node = _first_child_of_type(c, "symbol")
                if field_name_node is None:
                    continue
                children.append(
                    BufferSymbol(
                        name=_text(field_name_node, state.src),
                        kind=SymbolKind.FIELD,
                        range=_node_range(c),
                        selection_range=_node_range(field_name_node),
                        scope_path=scope_path_inside,
                        parent_label=None,
                        children=(),
                    )
                )

        out.append(
            BufferSymbol(
                name=name,
                kind=kind,
                range=_node_range(node),
                selection_range=_node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=tuple(children),
            )
        )

    def _handle_enum(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            # Anonymous enum (Gap #2 in the coverage doc) — grammar already
            # rejects it with ERROR. Skip.
            return
        name = _text(name_node, state.src)
        children: list[BufferSymbol] = []
        scope_path_inside = self._current_scope_path(state) + (name,)
        for c in node.children:
            if c.type == "pseudo_inst_enum_field":
                field_name_node = _first_child_of_type(c, "symbol")
                if field_name_node is None:
                    continue
                children.append(
                    BufferSymbol(
                        name=_text(field_name_node, state.src),
                        kind=SymbolKind.FIELD,
                        range=_node_range(c),
                        selection_range=_node_range(field_name_node),
                        scope_path=scope_path_inside,
                        parent_label=None,
                        children=(),
                    )
                )

        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.ENUM,
                range=_node_range(node),
                selection_range=_node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=tuple(children),
            )
        )

    # ---- single-statement handlers ---------------------------------- #

    def _handle_plain_label(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        body = _first_child_of_type(node, "label_body")
        if body is None:
            return
        name = _text(body, state.src)
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.LABEL,
                range=_node_range(node),
                selection_range=_node_range(body),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=(),
            )
        )
        # A plain label becomes the cheap-local anchor for subsequent
        # @cheap labels in this scope.
        state.parent_label = name

    def _handle_cheap_local(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # local_label > local_label_body (which starts with '@' then identifier)
        body = _first_child_of_type(node, "local_label_body")
        if body is None:
            return
        # Keep the leading "@" in the name — preserves the syntactic signal
        # that this is a cheap local, so client UIs don't conflate it with a
        # top-level label of the same suffix.
        name = _text(body, state.src)
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.CHEAP_LOCAL,
                range=_node_range(node),
                selection_range=_node_range(body),
                scope_path=self._current_scope_path(state),
                parent_label=state.parent_label,
                children=(),
            )
        )

    def _handle_anon_label(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        out.append(
            BufferSymbol(
                name=":",
                kind=SymbolKind.ANON_LABEL,
                range=_node_range(node),
                selection_range=_node_range(node),
                scope_path=self._current_scope_path(state),
                parent_label=state.parent_label,
                children=(),
            )
        )

    def _handle_segment(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # .segment "NAME" — find the string child and strip quotes.
        str_node = _first_child_of_type(node, "string")
        if str_node is None:
            return
        raw = _text(str_node, state.src).strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            name = raw[1:-1]
        else:
            name = raw
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.SEGMENT,
                range=_node_range(node),
                selection_range=_node_range(str_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=(),
            )
        )

    def _handle_constant(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # symbol_eq / symbol_assign: first child is a ``symbol`` (the LHS).
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            return
        out.append(
            BufferSymbol(
                name=_text(name_node, state.src),
                kind=SymbolKind.CONSTANT,
                range=_node_range(node),
                selection_range=_node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=(),
            )
        )

    def _handle_import_export(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
        *,
        kind: SymbolKind,
        symbol_child_type: str,
    ) -> None:
        # Comma-separated declarators. Each declarator's first ``symbol`` child
        # is the imported/exported name (LHS of an optional ``:= expr``).
        scope_path = self._current_scope_path(state)
        for c in node.children:
            if c.type != symbol_child_type:
                continue
            name_node = _first_child_of_type(c, "symbol")
            if name_node is None:
                # Some import/export wrappers are the bare ``symbol`` itself.
                if c.type == symbol_child_type and c.named_child_count == 0:
                    continue
                continue
            out.append(
                BufferSymbol(
                    name=_text(name_node, state.src),
                    kind=kind,
                    range=_node_range(c),
                    selection_range=_node_range(name_node),
                    scope_path=scope_path,
                    parent_label=None,
                    children=(),
                )
            )

    def _handle_global(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # ``.global``/``.globalzp`` puts the symbols as direct ``symbol``
        # children of the pseudo_inst node (not wrapped).
        scope_path = self._current_scope_path(state)
        # Treat these as EXPORT-class declarators for the indexer's purposes
        # (they're combined import-or-export).
        for c in node.children:
            if c.type == "symbol":
                out.append(
                    BufferSymbol(
                        name=_text(c, state.src),
                        kind=SymbolKind.EXPORT,
                        range=_node_range(c),
                        selection_range=_node_range(c),
                        scope_path=scope_path,
                        parent_label=None,
                        children=(),
                    )
                )

    # ------------------------------------------------------------------ #
    # references                                                          #
    # ------------------------------------------------------------------ #

    def _definition_spans(self) -> set[tuple[int, int]]:
        """Collect byte-ranges of every name-node that *defines* a
        symbol. Used to exclude those from reference output.
        """
        src = self._text.encode("utf-8")
        spans: set[tuple[int, int]] = set()
        self._collect_def_spans(self._tree.root_node, src, spans)
        return spans

    def _collect_def_spans(self, node: Node, src: bytes, out: set[tuple[int, int]]) -> None:
        t = node.type
        if t in ("pseudo_inst_proc", "pseudo_inst_scope"):
            wrapper_type = (
                "pseudo_inst_proc_symbol" if t == "pseudo_inst_proc" else "pseudo_inst_scope_symbol"
            )
            wrapper = _first_child_of_type(node, wrapper_type)
            sym = _find_descendant_symbol(wrapper) if wrapper else None
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t in (
            "pseudo_inst_macro",
            "pseudo_inst_struct",
            "pseudo_inst_union",
            "pseudo_inst_enum",
        ):
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t in ("symbol_eq", "symbol_assign"):
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t == "label":
            body = _first_child_of_type(node, "label_body")
            if body is not None:
                out.add((body.start_byte, body.end_byte))
        elif t == "local_label":
            body = _first_child_of_type(node, "local_label_body")
            if body is not None:
                out.add((body.start_byte, body.end_byte))
        elif t in (
            "pseudo_inst_import_symbol",
            "pseudo_inst_importzp_symbol",
            "pseudo_inst_export_symbol",
            "pseudo_inst_exportzp_symbol",
        ):
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t == "pseudo_inst_struct_or_union_field":
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t == "pseudo_inst_enum_field":
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        for c in node.children:
            self._collect_def_spans(c, src, out)

    def _walk_references(
        self,
        node: Node,
        src: bytes,
        target: str,
        defn_spans: set[tuple[int, int]],
        out: list[SymbolReference],
        scope: tuple[str, ...],
    ) -> None:
        # Track scope as we descend; macros do not form a separate scope.
        new_scope = scope
        if node.type == "pseudo_inst_proc":
            wrapper = _first_child_of_type(node, "pseudo_inst_proc_symbol")
            sym = _find_descendant_symbol(wrapper) if wrapper else None
            if sym is not None:
                new_scope = scope + (_text(sym, src),)
        elif node.type == "pseudo_inst_scope":
            wrapper = _first_child_of_type(node, "pseudo_inst_scope_symbol")
            sym = _find_descendant_symbol(wrapper) if wrapper else None
            if sym is not None:
                new_scope = scope + (_text(sym, src),)

        # Identifier-shaped reference nodes.
        if node.type == "symbol":
            if (node.start_byte, node.end_byte) not in defn_spans:
                if _text(node, src) == target:
                    out.append(
                        SymbolReference(
                            name=target,
                            uri=self.uri,
                            range=_node_range(node),
                            scope_path=scope,
                        )
                    )
        elif node.type == "local_label_literal":
            # ``@name`` reference — match against the full @-prefixed name
            # since we now preserve the '@' in BufferSymbol.name (see
            # _handle_cheap_local).
            if _text(node, src) == target:
                out.append(
                    SymbolReference(
                        name=target,
                        uri=self.uri,
                        range=_node_range(node),
                        scope_path=scope,
                    )
                )
        elif node.type == "macro_inst_name":
            # A macro_inst's name — only a reference if it matches a known
            # macro definition. Otherwise per Gap #4 it's a stray label-like
            # token; we still emit it as a reference so the indexer can wire
            # it up.
            if _text(node, src) == target:
                out.append(
                    SymbolReference(
                        name=target,
                        uri=self.uri,
                        range=_node_range(node),
                        scope_path=scope,
                    )
                )

        for c in node.children:
            self._walk_references(c, src, target, defn_spans, out, new_scope)

    def _walk_all_references(
        self,
        node: Node,
        src: bytes,
        defn_spans: set[tuple[int, int]],
        out: list[SymbolReference],
        scope: tuple[str, ...],
    ) -> None:
        """Single-pass variant of ``_walk_references`` that emits a record for
        *every* identifier-shaped reference, not just those matching one
        target name.  Used by :meth:`all_references` to avoid the O(N) re-walk
        cost of calling :meth:`references_in` once per name during full
        workspace indexing.
        """
        new_scope = scope
        if node.type == "pseudo_inst_proc":
            wrapper = _first_child_of_type(node, "pseudo_inst_proc_symbol")
            sym = _find_descendant_symbol(wrapper) if wrapper else None
            if sym is not None:
                new_scope = scope + (_text(sym, src),)
        elif node.type == "pseudo_inst_scope":
            wrapper = _first_child_of_type(node, "pseudo_inst_scope_symbol")
            sym = _find_descendant_symbol(wrapper) if wrapper else None
            if sym is not None:
                new_scope = scope + (_text(sym, src),)

        # Identifier-shaped reference nodes.  Same node-type set as
        # ``_walk_references``; the only difference is no target-name filter.
        t = node.type
        if t == "symbol":
            if (node.start_byte, node.end_byte) not in defn_spans:
                name = _text(node, src)
                out.append(SymbolReference(name=name, uri=self.uri, range=_node_range(node), scope_path=scope))
        elif t == "local_label_literal":
            name = _text(node, src)
            out.append(SymbolReference(name=name, uri=self.uri, range=_node_range(node), scope_path=scope))
        elif t == "macro_inst_name":
            name = _text(node, src)
            out.append(SymbolReference(name=name, uri=self.uri, range=_node_range(node), scope_path=scope))

        for c in node.children:
            self._walk_all_references(c, src, defn_spans, out, new_scope)

    # ------------------------------------------------------------------ #
    # helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _current_scope_path(state: _CollectState) -> tuple[str, ...]:
        return tuple(name for name, _is_anchor in state.scope_stack)


# ============================================================================ #
# Post-process: synthesize body ranges for `label:`-style routines.            #
# ============================================================================ #
#
# Reason: ca65 codebases (like the user's c64-https) commonly write routines as
#
#     hkdf_extract:
#             ; ... body ...
#             rts
#
# rather than with ``.proc Name ... .endproc``.  Without help, our tree-sitter
# pass emits each label as a single-line ``BufferSymbol``, which breaks:
#   * ``find_symbol(include_body=True)`` — body never spans the routine
#   * outline-rendering — every cheap local sits at the top level, not nested
#   * cheap-local ``parent_label`` is correct but invisible in outlines
#
# This pass scans the sibling list at each scope level.  For each ``LABEL``
# symbol it:
#   1. extends ``range.end`` to the line immediately before the next sibling
#      *boundary* (next ``LABEL`` / ``.proc`` / ``.scope`` / ``.macro`` / etc.,
#      or EOF), and
#   2. absorbs any ``CHEAP_LOCAL`` / ``ANON_LABEL`` siblings that fall inside
#      the new body range into the label's ``children`` tuple (with their
#      ``parent_label`` set to the label's name).
#
# Plain non-cheap labels are NOT nested under other plain labels — CA65 has no
# syntactic grouping between sibling labels, so we keep them as siblings.  If
# the user wants explicit grouping, ``.proc`` does that.


_BOUNDARY_KINDS = frozenset({
    SymbolKind.LABEL,
    SymbolKind.PROC,
    SymbolKind.SCOPE,
    SymbolKind.MACRO,
    SymbolKind.STRUCT,
    SymbolKind.UNION,
    SymbolKind.ENUM,
    SymbolKind.SEGMENT,
})

_ABSORBED_KINDS = frozenset({
    SymbolKind.CHEAP_LOCAL,
    SymbolKind.ANON_LABEL,
})


def _synthesize_label_bodies(
    syms: list[BufferSymbol],
    src_lines: list[str],
) -> list[BufferSymbol]:
    """Apply the body-range + cheap-local-absorption pass.

    Recurses into container children (``.proc`` etc.) so label-style routines
    embedded inside a ``.scope`` get the same treatment.
    """
    if not syms:
        return syms

    # First recurse so nested label-style routines get fixed too.
    syms = [
        replace(s, children=tuple(_synthesize_label_bodies(list(s.children), src_lines)))
        if s.children
        else s
        for s in syms
    ]

    n = len(syms)

    # Pre-compute, for each index i, the smallest j >= i such that syms[j] is
    # a boundary (or n if none).  A label at index i then has its body extend
    # up to next_boundary[i + 1] -- the next sibling that ends the routine.
    # Order matters: we update `last` BEFORE recording so the boundary at i
    # itself counts.
    next_boundary: list[int] = [n] * n
    last = n
    for i in range(n - 1, -1, -1):
        if syms[i].kind in _BOUNDARY_KINDS:
            last = i
        next_boundary[i] = last

    result: list[BufferSymbol] = []
    absorbed_indices: set[int] = set()

    for i, s in enumerate(syms):
        if i in absorbed_indices:
            continue
        if s.kind != SymbolKind.LABEL:
            result.append(s)
            continue

        nb = next_boundary[i + 1] if (i + 1) < n else n

        # Collect cheap-locals and anonymous-labels between (i, nb).
        new_children: list[BufferSymbol] = list(s.children)
        for j in range(i + 1, nb):
            other = syms[j]
            if other.kind in _ABSORBED_KINDS:
                new_children.append(replace(other, parent_label=s.name))
                absorbed_indices.add(j)

        # Compute the body end position: the end-of-line of the line before
        # the next boundary, or EOF.
        start_line = s.range.start.line
        if nb < n:
            boundary_line = syms[nb].range.start.line
            end_line = max(start_line, boundary_line - 1)
        else:
            end_line = max(start_line, len(src_lines) - 1)

        end_char = len(src_lines[end_line]) if 0 <= end_line < len(src_lines) else 0
        new_range = Range(
            start=s.range.start,
            end=Position(line=end_line, character=end_char),
        )

        result.append(replace(s, range=new_range, children=tuple(new_children)))

    return result
