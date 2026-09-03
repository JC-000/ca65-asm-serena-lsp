"""
Buffer-layer Document: parses a single CA65 source file with tree-sitter
and produces a list of :class:`BufferSymbol` records the Indexer can consume.

Grammar: ``pogyomo/tree-sitter-ca65`` pinned at commit ``b22ead1``, vendored
under ``vendor/tree-sitter-ca65/`` and compiled into this package as
``ca65_ls._grammar`` (see the NOTICE.md there for provenance and update
procedure); it exposes the same ``language()`` factory as the upstream
Python package, which the ``tree_sitter`` runtime wraps in a :class:`Language`.

See ``docs/research/ts-ca65-coverage.md`` for the grammar coverage report.
The grammar gaps we cope with here, and how:

* **Gap #4** (macro/label ambiguity at statement position) — anything that
  parses as ``macro_inst`` whose name does *not* match a ``.macro``
  definition in the same file is downgraded to a plain label reference.
  Macro invocations are never emitted as symbols, except under
  ``.feature labels_without_colons`` (below).
* **Gap #1** (leading ``::name`` global-scope operator) — the grammar turns
  it into an ``ERROR`` that swallows the next statement.  We blank the two
  colons in the *parse input* (never in the text we report positions for),
  which is byte-length preserving, so ``.if ::FLAG`` parses as ``.if FLAG``
  and the reference to ``FLAG`` lands at its real column.
* **Address-size prefixes** ``a:``/``z:``/``f:`` (``lda a:bar``) parse as an
  anonymous label followed by a stray macro call.  Same trick: the prefix
  is blanked in the parse input when it sits in operand position.
* **``.feature labels_without_colons``** — the grammar has no notion of the
  feature, so every ``Name  inst`` line is a macro call.  When a file
  declares the feature we replace the first blank after a column-0
  identifier with ``:`` in the parse input, and treat a bare column-0
  identifier line as a label.
* **Macro arguments** are opaque ``*_arg_raw`` text to the grammar; we
  tokenise them ourselves for references.
* The remaining gaps (65C02/65816 mnemonics, anonymous ``.enum``, block
  comments) do not affect our synthetic corpus and are noted only.

Columns: tree-sitter reports **byte** columns; LSP wants UTF-16 code units,
and the server's string handling is code-point based.  Every ``Range`` this
module emits is converted to code-point columns through :class:`_LineMap`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from tree_sitter import Language, Node, Parser, Tree

from ca65_ls import _grammar as _ts_ca65

from ..types import BufferSymbol, Position, Range, SymbolKind, SymbolReference

_log = logging.getLogger(__name__)


# --- single shared Language / Parser ------------------------------------- #

_LANGUAGE: Language | None = None


def _get_language() -> Language:
    global _LANGUAGE
    if _LANGUAGE is None:
        _LANGUAGE = Language(_ts_ca65.language())
    return _LANGUAGE


def _new_parser() -> Parser:
    return Parser(_get_language())


# --- byte column -> character column ------------------------------------- #


class _LineMap:
    """Convert tree-sitter ``(row, byte_column)`` points into code-point
    columns for the same source.

    Lines that are pure ASCII (the overwhelming majority) map 1:1 and are
    flagged once, so the conversion costs nothing there; other lines decode
    the byte prefix on demand.
    """

    __slots__ = ("_src", "_starts", "_ascii")

    def __init__(self, src: bytes) -> None:
        self._src = src
        starts = [0]
        for i, b in enumerate(src):
            if b == 0x0A:
                starts.append(i + 1)
        self._starts = starts
        self._ascii = [
            src[a:b].isascii() for a, b in zip(starts, starts[1:] + [len(src)], strict=True)
        ]

    def position(self, point: tuple[int, int]) -> Position:
        row, bcol = point
        if row >= len(self._starts):
            return Position(line=row, character=bcol)
        if self._ascii[row]:
            return Position(line=row, character=bcol)
        start = self._starts[row]
        prefix = self._src[start : start + bcol].decode("utf-8", errors="replace")
        return Position(line=row, character=len(prefix))

    def node_range(self, node: Node) -> Range:
        return Range(start=self.position(node.start_point), end=self.position(node.end_point))

    def byte_range(self, start_byte: int, end_byte: int) -> Range:
        """Range for an arbitrary byte span (used for tokens we lex ourselves
        inside raw macro-argument text)."""
        return Range(start=self._byte_pos(start_byte), end=self._byte_pos(end_byte))

    def _byte_pos(self, offset: int) -> Position:
        # Binary search the line containing ``offset``.
        lo, hi = 0, len(self._starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return self.position((lo, offset - self._starts[lo]))


# --- internal helpers ---------------------------------------------------- #


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_child_of_type(node: Node, type_name: str) -> Node | None:
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_descendant_symbol(node: Node) -> Node | None:
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

#: Register names that can never be symbols (see CLAUDE.md gotcha #1).
_RESERVED_IDENTS = frozenset({"a", "x", "y", "s"})


# --- dialect sniff --------------------------------------------------------- #

# ACME directives at line start.  A ``.asm``/``.s`` extension says nothing
# about the dialect: c64-nist-curves keeps ACME and CA65 twins side by side
# and c64-sid-instruments is ACME throughout.
_ACME_DIRECTIVE_RE = re.compile(
    r"^[ \t]*!(?:zone|zn|byte|by|word|wo|text|tx|pet|scr|raw|to|cpu|source|src|macro|if|ifdef"
    r"|ifndef|fill|fi|align|convtab|ct|set|addr|initmem|8|16|24|32|bin|binary|for|do|while"
    r"|warn|error|serious|pseudopc|realpc|symbollist|sl)\b",
    re.IGNORECASE | re.MULTILINE,
)
# CA65 control commands at line start (an explicit list, because ACME's own
# ``.local`` labels also start with a dot).
_CA65_DIRECTIVE_RE = re.compile(
    r"^[ \t]*\.(?:proc|endproc|scope|endscope|segment|macro|mac|endmacro|endmac|import|export"
    r"|importzp|exportzp|include|incbin|res|byte|byt|word|dbyt|dword|addr|setcpu|feature|org"
    r"|code|data|bss|zeropage|rodata|if|ifdef|ifndef|ifblank|ifnblank|ifconst|ifref|ifdef|else"
    r"|elseif|endif|struct|endstruct|union|endunion|enum|endenum|repeat|endrepeat|endrep|define"
    r"|global|globalzp|local|macpack|assert|error|warning|out|pushseg|popseg|align|asciiz"
    r"|lobytes|hibytes|tag|constructor|destructor|interruptor|autoimport|p02|pc02|p816|a8|a16"
    r"|i8|i16|smart|case|charmap|reloc|forceimport|undefine|undef|delmacro|delmac|exitmacro"
    r"|exitmac|literal|pagelength|pagelen|listbytes|list|null|sunplus|psc02|p4510)\b",
    re.IGNORECASE | re.MULTILINE,
)


#: A comment runs from an unquoted ``;`` to end of line in both dialects.
_COMMENT_STRIP_RE = re.compile(r";[^\n]*")


def looks_like_acme(text: str) -> bool:
    """True when the source is ACME rather than CA65.

    Such a file must not be handed to the CA65 grammar: it would parse into
    partial garbage symbols (c64-nist-curves keeps ACME and CA65 twins side
    by side, `mod256.asm` next to `mod256.s`).

    Comments are stripped before sniffing.  A false positive silently empties
    a real source file, and the adversarial review of 2026-09-02 found that
    a CA65 file whose only ``!`` line sat in a comment, or which simply used
    no dotted directive, was classified ACME.  After stripping comments a
    remaining ``!directive`` at line start is genuine ACME syntax (it is a
    syntax error in CA65), so one is enough -- c64-nist-curves' `fp384.asm`
    is ACME on the strength of a single ``!fill``.
    """
    body = _COMMENT_STRIP_RE.sub("", text)
    if not _ACME_DIRECTIVE_RE.search(body):
        return False
    return _CA65_DIRECTIVE_RE.search(body) is None


# --- parse-input rewrites (byte-length preserving) --------------------------- #
#
# Each rewrite below patches the *bytes handed to tree-sitter* without
# changing their length, so every node position still addresses the original
# text.  Identifiers are never touched, so ``_text(node, src)`` on a name node
# is exact; only punctuation that the grammar cannot handle is blanked.

# Leading ``::name`` (global-scope operator).  ``foo::bar`` (a ``member``
# node the grammar does understand) is untouched thanks to the look-behind.
_GLOBAL_SCOPE_RE = re.compile(rb"(?<![A-Za-z0-9_@:])::(?=[A-Za-z_])")

# ``a:``/``z:``/``f:`` address-size prefixes in operand position.  Guarded by
# a look-behind for operand punctuation; a second check in the rewrite
# requires *something* before the prefix on the line, so an indented label
# named ``z:`` is left alone.
_ADDR_SIZE_RE = re.compile(rb"(?<=[ \t,(#\[])[azfAZF]:(?=[ \t]*[A-Za-z_$%(<>@.0-9])")

# ``.feature labels_without_colons`` anywhere in the file (also accepts the
# comma-separated form and any case).
_LABELS_WITHOUT_COLONS_RE = re.compile(
    rb"^[ \t]*\.feature\b[^\n;]*\blabels_without_colons\b", re.IGNORECASE | re.MULTILINE
)
_COLONLESS_LABEL_RE = re.compile(rb"^(@?[A-Za-z_][A-Za-z0-9_]*)[ \t]")
_COLONLESS_NOT_LABEL_RE = re.compile(rb"[=:]|\.set\b", re.IGNORECASE)
_MACRO_DEF_RE = re.compile(rb"^[ \t]*\.mac(?:ro)?[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.I | re.M)

# 65C02 / 65816 mnemonics the vendored grammar does not know (it stops at the
# NMOS 6502 set); needed so a column-0 instruction in a colon-less file is
# not mistaken for a label.
_EXTRA_MNEMONICS = frozenset(
    {
        "bra",
        "phx",
        "phy",
        "plx",
        "ply",
        "stz",
        "trb",
        "tsb",
        "bbr",
        "bbs",
        "rmb",
        "smb",
        "stp",
        "wai",
        "brl",
        "cop",
        "jml",
        "jsl",
        "mvn",
        "mvp",
        "pea",
        "pei",
        "per",
        "phb",
        "phd",
        "phk",
        "plb",
        "pld",
        "rep",
        "rtl",
        "sep",
        "tcd",
        "tcs",
        "tdc",
        "tsc",
        "txy",
        "tyx",
        "wdm",
        "xba",
        "xce",
    }
)


def _grammar_mnemonics() -> frozenset[str]:
    lang = _get_language()
    kinds = (lang.node_kind_for_id(i) for i in range(lang.node_kind_count))
    return frozenset(k[len("opcode_") :] for k in kinds if k and k.startswith("opcode_"))


_MNEMONICS: frozenset[str] | None = None


def _mnemonics() -> frozenset[str]:
    global _MNEMONICS
    if _MNEMONICS is None:
        _MNEMONICS = _grammar_mnemonics() | _EXTRA_MNEMONICS
    return _MNEMONICS


def _rewrite_global_scope(src: bytes) -> bytes:
    return _GLOBAL_SCOPE_RE.sub(b"  ", src)


def _rewrite_addr_size_prefixes(src: bytes) -> bytes:
    if b":" not in src:
        return src
    out = bytearray(src)
    line_start = 0
    for m in _ADDR_SIZE_RE.finditer(src):
        line_start = src.rfind(b"\n", 0, m.start()) + 1
        if not src[line_start : m.start()].strip():
            continue  # first token on the line: could be a label
        out[m.start() : m.end()] = b"  "
    return bytes(out)


def _rewrite_colonless_labels(src: bytes) -> bytes:
    """``Name  inst`` at column 0 -> ``Name:inst`` when the file enables
    ``labels_without_colons``.  Column-0 identifiers that are instruction
    mnemonics, macros defined in this file, or assignments are skipped."""
    macro_names = {m.group(1).decode("ascii").lower() for m in _MACRO_DEF_RE.finditer(src)}
    mnemonics = _mnemonics()
    out = bytearray(src)
    offset = 0
    for line in src.split(b"\n"):
        m = _COLONLESS_LABEL_RE.match(line)
        if m is not None:
            name = m.group(1).decode("ascii").lower()
            rest = line[m.end() :].lstrip()
            if (
                name not in mnemonics
                and name not in macro_names
                and not _COLONLESS_NOT_LABEL_RE.match(rest)
            ):
                out[offset + m.end() - 1] = 0x3A  # ':'
        offset += len(line) + 1
    return bytes(out)


def _prepare_parse_input(text: str) -> tuple[bytes, bool]:
    """Return ``(bytes for tree-sitter, labels_without_colons?)``."""
    src = text.encode("utf-8")
    src = _rewrite_global_scope(src)
    src = _rewrite_addr_size_prefixes(src)
    colonless = _LABELS_WITHOUT_COLONS_RE.search(src) is not None
    if colonless:
        src = _rewrite_colonless_labels(src)
    return src, colonless


# --- raw macro-argument lexing ------------------------------------------------ #

# Tokens inside ``macro_inst_arg_raw`` / ``macro_call_arg_raw``: strings are
# skipped, ``.func``-style names and numbers are skipped, identifiers are
# references, and a ``;`` ends the useful part of the line.
_RAW_ARG_TOKEN_RE = re.compile(
    rb'"[^"\n]*"?|\'[^\'\n]*\'?'
    rb"|(?P<comment>;)"
    rb"|(?P<dot>\.[A-Za-z_][A-Za-z0-9_]*)"
    rb"|(?P<num>\$[0-9A-Fa-f]+|%[01]+|[0-9][0-9A-Za-z_]*)"
    rb"|(?P<ident>@?[A-Za-z_][A-Za-z0-9_]*)"
)


_IDENT_RE = re.compile(rb"@?[A-Za-z_][A-Za-z0-9_]*")


def _raw_arg_identifiers(raw: bytes, base: int) -> list[tuple[str, int, int]]:
    """Yield ``(name, start_byte, end_byte)`` for every identifier in a raw
    argument span starting at absolute byte ``base``."""
    out: list[tuple[str, int, int]] = []
    for m in _RAW_ARG_TOKEN_RE.finditer(raw):
        if m.group("comment") is not None:
            break
        if m.group("ident") is None:
            continue
        name = m.group("ident").decode("ascii")
        lowered = name.lower()
        if lowered in _RESERVED_IDENTS or lowered in _mnemonics():
            continue
        out.append((name, base + m.start(), base + m.end()))
    return out


# --- the Document --------------------------------------------------------- #


@dataclass
class _CollectState:
    """Mutable state threaded through the recursive collector."""

    src: bytes
    lm: _LineMap
    macro_defs: set[str]
    colonless_labels: bool
    # Stack of current scope path components: ``(name, is_cheap_anchor)``.
    # ``is_cheap_anchor`` is True when the enclosing context anchors cheap
    # locals (``.proc``, ``.scope``, or a plain label).
    scope_stack: list[tuple[str, bool]]
    # Current enclosing non-cheap label name (for ``parent_label`` on
    # cheap locals).  None at top level.
    parent_label: str | None


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

    __slots__ = (
        "uri",
        "_text",
        "_src",
        "_lm",
        "_colonless",
        "_acme",
        "_parser",
        "_tree",
        "_symbols",
        "_flat",
        "_all_refs",
        "_macro_defs",
        "_defined_names",
    )

    def __init__(self, uri: str, text: str) -> None:
        self.uri = uri
        self._text = text
        self._src, self._colonless = _prepare_parse_input(text)
        self._lm = _LineMap(self._src)
        self._acme = looks_like_acme(text)
        self._parser = _new_parser()
        self._tree: Tree = self._parser.parse(self._src)
        self._symbols: tuple[BufferSymbol, ...] = ()
        self._flat: tuple[BufferSymbol, ...] = ()
        self._all_refs: tuple[SymbolReference, ...] | None = None
        self._macro_defs: set[str] = set()
        self._defined_names: set[str] = set()
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
    def is_acme(self) -> bool:
        """True when the file was sniffed as ACME dialect and skipped."""
        return self._acme

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
        out: list[SymbolReference] = []
        if not self._acme:
            defn_spans = self._definition_spans()
            self._walk_all_references(self._tree.root_node, defn_spans, out, scope=())
        self._all_refs = tuple(out)
        return list(out)

    def update(self, new_text: str) -> None:
        """Re-parse with incremental help from the old tree.

        We don't have edit-deltas in this minimal API, so we apply a
        whole-buffer edit and let tree-sitter's incremental parser
        decide what to reuse.
        """
        old_src = self._src
        new_src, colonless = _prepare_parse_input(new_text)

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
        self._src = new_src
        self._colonless = colonless
        self._lm = _LineMap(new_src)
        self._acme = looks_like_acme(new_text)
        self._tree = self._parser.parse(new_src, self._tree)
        self._symbols = ()
        self._flat = ()
        self._all_refs = None
        self._macro_defs = set()
        self._defined_names = set()
        self._extract()

    # ------------------------------------------------------------------ #
    # extraction                                                          #
    # ------------------------------------------------------------------ #

    def _extract(self) -> None:
        if self._acme:
            _log.debug("%s looks like ACME dialect; emitting no symbols", self.uri)
            return
        src = self._src
        root = self._tree.root_node
        self._log_errors(root)

        # First pass: collect macro definition names (file-local). Needed to
        # disambiguate macro_inst vs label-reference per Gap #4.
        macro_defs: set[str] = set()
        self._collect_macro_defs(root, src, macro_defs)
        self._macro_defs = macro_defs

        state = _CollectState(
            src=src,
            lm=self._lm,
            macro_defs=macro_defs,
            colonless_labels=self._colonless,
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
        #
        # Lines are split on "\n" only, matching tree-sitter's row counting
        # (str.splitlines would also break on \r, \f, \x1c... and drift).
        src_lines = [ln.rstrip("\r") for ln in self._text.split("\n")]
        if len(src_lines) > 1 and src_lines[-1] == "":
            src_lines.pop()  # a trailing newline is not an extra line
        top = _synthesize_label_bodies(top, src_lines, None)

        # Post-process: drop ``.export``/``.exportzp``/``.global`` declarators
        # whose target is also *defined* in this same file.  The declarator and
        # the definition are the same entity, so emitting both made every
        # exported routine show up twice in document symbols (once as a
        # one-token EXPORT, once as the real PROC/LABEL/CONSTANT).  The
        # declarator's name token is then reported as a *reference* instead
        # (see ``_collect_def_spans``), so rename still edits the line.
        defined: set[str] = set()
        _collect_definition_names(top, defined)
        self._defined_names = defined
        if defined:
            top = _prune_exports(top, defined)

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

    def _is_colonless_label(self, node: Node) -> bool:
        """A bare column-0 ``macro_inst`` in a ``labels_without_colons`` file
        that names no macro defined here is a label (``Name`` alone on its
        line; the ``Name  inst`` form is rewritten before parsing)."""
        return (
            self._colonless
            and node.type == "macro_inst"
            and node.start_point[1] == 0
            and _first_child_of_type(node, "macro_inst_arg_raw") is None
            and (
                (name := _first_child_of_type(node, "macro_inst_name")) is not None
                and _text(name, self._src) not in self._macro_defs
            )
        )

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
        if node_type == "local_label_body" and state.colonless_labels and node.start_point[1] == 0:
            # ``@name`` alone on a line under labels_without_colons: the
            # grammar leaves the body inside an ERROR node.
            self._emit_cheap_local(node, state, out)
            return
        if node_type == "unnamed_label":
            self._handle_anon_label(node, state, out)
            return
        if node_type == "pseudo_inst_segment":
            self._handle_segment(node, state, out)
            return
        if node_type in ("symbol_eq", "symbol_assign", "symbol_set"):
            self._handle_constant(node, state, out)
            return
        if node_type == "pseudo_inst_define":
            self._handle_define(node, state, out)
            return
        if node_type in (
            "pseudo_inst_import",
            "pseudo_inst_importzp",
        ):
            self._handle_import_export(
                node,
                state,
                out,
                kind=SymbolKind.IMPORT,
                symbol_child_type=(
                    "pseudo_inst_import_symbol"
                    if node_type == "pseudo_inst_import"
                    else "pseudo_inst_importzp_symbol"
                ),
            )
            return
        if node_type in (
            "pseudo_inst_export",
            "pseudo_inst_exportzp",
        ):
            self._handle_import_export(
                node,
                state,
                out,
                kind=SymbolKind.EXPORT,
                symbol_child_type=(
                    "pseudo_inst_export_symbol"
                    if node_type == "pseudo_inst_export"
                    else "pseudo_inst_exportzp_symbol"
                ),
            )
            return
        if node_type in ("pseudo_inst_global", "pseudo_inst_globalzp"):
            self._handle_global(node, state, out)
            return

        # macro_inst at statement position: NOT a symbol (per Gap #4 we
        # only emit definitions) — unless it is really a colon-less label.
        if node_type == "macro_inst":
            if self._is_colonless_label(node):
                self._handle_colonless_label(node, state, out)
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
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
                range=state.lm.node_range(node),
                selection_range=(
                    state.lm.node_range(name_node) if name_node else state.lm.node_range(node)
                ),
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
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
                        range=state.lm.node_range(c),
                        selection_range=state.lm.node_range(field_name_node),
                        scope_path=scope_path_inside,
                        parent_label=None,
                        children=(),
                    )
                )

        out.append(
            BufferSymbol(
                name=name,
                kind=kind,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
                        range=state.lm.node_range(c),
                        selection_range=state.lm.node_range(field_name_node),
                        scope_path=scope_path_inside,
                        parent_label=None,
                        children=(),
                    )
                )

        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.ENUM,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
        self._emit_label(body, node, state, out)

    def _handle_colonless_label(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        name_node = _first_child_of_type(node, "macro_inst_name")
        if name_node is not None:
            self._emit_label(name_node, node, state, out)

    def _emit_label(
        self,
        name_node: Node,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        name = _text(name_node, state.src)
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.LABEL,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
        self._emit_cheap_local(body, state, out, node)

    def _emit_cheap_local(
        self,
        body: Node,
        state: _CollectState,
        out: list[BufferSymbol],
        node: Node | None = None,
    ) -> None:
        # Keep the leading "@" in the name — preserves the syntactic signal
        # that this is a cheap local, so client UIs don't conflate it with a
        # top-level label of the same suffix.
        name = _text(body, state.src)
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.CHEAP_LOCAL,
                range=state.lm.node_range(node if node is not None else body),
                selection_range=state.lm.node_range(body),
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
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(node),
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
        name = raw[1:-1] if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"' else raw
        out.append(
            BufferSymbol(
                name=name,
                kind=SymbolKind.SEGMENT,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(str_node),
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
        # symbol_eq / symbol_assign / symbol_set: first child is a ``symbol``
        # (the LHS).
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            return
        out.append(
            BufferSymbol(
                name=_text(name_node, state.src),
                kind=SymbolKind.CONSTANT,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
                scope_path=self._current_scope_path(state),
                parent_label=None,
                children=(),
            )
        )

    def _handle_define(
        self,
        node: Node,
        state: _CollectState,
        out: list[BufferSymbol],
    ) -> None:
        # ``.define NAME value`` is a constant-like text substitution;
        # ``.define NAME(args) body`` is a (single-line) macro.
        name_node = _first_child_of_type(node, "symbol")
        if name_node is None:
            return
        has_params = _first_child_of_type(node, "(") is not None
        out.append(
            BufferSymbol(
                name=_text(name_node, state.src),
                kind=SymbolKind.MACRO if has_params else SymbolKind.CONSTANT,
                range=state.lm.node_range(node),
                selection_range=state.lm.node_range(name_node),
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
                continue
            out.append(
                BufferSymbol(
                    name=_text(name_node, state.src),
                    kind=kind,
                    range=state.lm.node_range(c),
                    selection_range=state.lm.node_range(name_node),
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
                        range=state.lm.node_range(c),
                        selection_range=state.lm.node_range(c),
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
        spans: set[tuple[int, int]] = set()
        self._collect_def_spans(self._tree.root_node, self._src, spans)
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
            "symbol_eq",
            "symbol_assign",
            "symbol_set",
        ):
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t == "pseudo_inst_define":
            # The name and any parameter names are binding positions.
            for c in node.children:
                if c.type == "symbol":
                    out.add((c.start_byte, c.end_byte))
        elif t == "label":
            body = _first_child_of_type(node, "label_body")
            if body is not None:
                out.add((body.start_byte, body.end_byte))
        elif t == "local_label":
            body = _first_child_of_type(node, "local_label_body")
            if body is not None:
                out.add((body.start_byte, body.end_byte))
        elif t == "macro_inst" and self._is_colonless_label(node):
            name = _first_child_of_type(node, "macro_inst_name")
            if name is not None:
                out.add((name.start_byte, name.end_byte))
        elif t in (
            "pseudo_inst_import_symbol",
            "pseudo_inst_importzp_symbol",
            "pseudo_inst_struct_or_union_field",
            "pseudo_inst_enum_field",
        ):
            sym = _first_child_of_type(node, "symbol")
            if sym is not None:
                out.add((sym.start_byte, sym.end_byte))
        elif t in ("pseudo_inst_export_symbol", "pseudo_inst_exportzp_symbol"):
            # ``.export foo`` is a binding position only when it is the sole
            # thing this file says about ``foo`` (then it is kept as an EXPORT
            # symbol).  When ``foo`` is defined here the declarator symbol is
            # suppressed, and the name token must surface as a reference so
            # rename edits the ``.export`` line too.
            sym = _first_child_of_type(node, "symbol")
            if sym is not None and _text(sym, src) not in self._defined_names:
                out.add((sym.start_byte, sym.end_byte))
        elif t in ("pseudo_inst_global", "pseudo_inst_globalzp"):
            for c in node.children:
                if c.type == "symbol" and _text(c, src) not in self._defined_names:
                    out.add((c.start_byte, c.end_byte))
        for c in node.children:
            self._collect_def_spans(c, src, out)

    def _walk_all_references(
        self,
        node: Node,
        defn_spans: set[tuple[int, int]],
        out: list[SymbolReference],
        scope: tuple[str, ...],
    ) -> None:
        """Single-pass walk that emits a record for *every* identifier-shaped
        reference.  Used by :meth:`all_references`; :meth:`references_in`
        filters its output by name.
        """
        src = self._src
        lm = self._lm
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

        t = node.type
        if t == "symbol":
            if (node.start_byte, node.end_byte) not in defn_spans:
                out.append(
                    SymbolReference(
                        name=_text(node, src),
                        uri=self.uri,
                        range=lm.node_range(node),
                        scope_path=scope,
                    )
                )
        elif t == "local_label_literal":
            out.append(
                SymbolReference(
                    name=_text(node, src), uri=self.uri, range=lm.node_range(node), scope_path=scope
                )
            )
        elif t == "macro_inst_name":
            # A macro_inst's name is a reference to the macro — or, per Gap
            # #4, to a label the grammar mistook for a macro.  Either way the
            # indexer wires it up by name.  Colon-less labels are definitions
            # and sit in ``defn_spans``.
            if (node.start_byte, node.end_byte) not in defn_spans:
                out.append(
                    SymbolReference(
                        name=_text(node, src),
                        uri=self.uri,
                        range=lm.node_range(node),
                        scope_path=scope,
                    )
                )
        elif t in ("macro_inst_arg_raw", "macro_call_arg_raw"):
            # Opaque to the grammar; lex it ourselves.
            raw = src[node.start_byte : node.end_byte]
            for name, start, end in _raw_arg_identifiers(raw, node.start_byte):
                if (start, end) in defn_spans:
                    continue
                out.append(
                    SymbolReference(
                        name=name, uri=self.uri, range=lm.byte_range(start, end), scope_path=scope
                    )
                )
            return
        elif t == "ERROR" and node.child_count == 0:
            # Error recovery: a lone identifier the grammar could not place
            # (e.g. ``blk`` in ``.sizeof(blk)`` inside a macro argument) is
            # still a name the user can navigate from.
            raw = src[node.start_byte : node.end_byte]
            if _IDENT_RE.fullmatch(raw) and (node.start_byte, node.end_byte) not in defn_spans:
                name = raw.decode("ascii")
                if name.lower() not in _RESERVED_IDENTS and name.lower() not in _mnemonics():
                    out.append(
                        SymbolReference(
                            name=name, uri=self.uri, range=lm.node_range(node), scope_path=scope
                        )
                    )

        for c in node.children:
            self._walk_all_references(c, defn_spans, out, new_scope)

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
#      *boundary* (next ``LABEL`` / ``.proc`` / ``.scope`` / ``.macro`` / etc.),
#      or, failing that, to the end of the enclosing container's body (the
#      line before ``.endproc``), or EOF at file level; and
#   2. absorbs any ``CHEAP_LOCAL`` / ``ANON_LABEL`` siblings that fall inside
#      the new body range into the label's ``children`` tuple (with their
#      ``parent_label`` set to the label's name).
#
# Plain non-cheap labels are NOT nested under other plain labels — CA65 has no
# syntactic grouping between sibling labels, so we keep them as siblings.  If
# the user wants explicit grouping, ``.proc`` does that.


_BOUNDARY_KINDS = frozenset(
    {
        SymbolKind.LABEL,
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.MACRO,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.ENUM,
        SymbolKind.SEGMENT,
    }
)

_ABSORBED_KINDS = frozenset(
    {
        SymbolKind.CHEAP_LOCAL,
        SymbolKind.ANON_LABEL,
    }
)


#: Symbol kinds that can be the target of a ``.export``.  An EXPORT declarator
#: naming one of these in the same file is redundant with the definition and is
#: dropped by :func:`_suppress_redundant_exports`.  Deliberately excludes
#: CHEAP_LOCAL / ANON_LABEL / FIELD / SEGMENT, which cannot be exported and
#: whose names could otherwise collide with a genuine re-export.
_EXPORTABLE_DEFINITION_KINDS: frozenset[SymbolKind] = frozenset(
    (
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.MACRO,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.ENUM,
        SymbolKind.LABEL,
        SymbolKind.CONSTANT,
    )
)


def _collect_definition_names(syms: list[BufferSymbol], out: set[str]) -> None:
    """Recursively gather the names of every symbol that *defines* an
    exportable entity."""
    for s in syms:
        if s.kind in _EXPORTABLE_DEFINITION_KINDS:
            out.add(s.name)
        if s.children:
            _collect_definition_names(list(s.children), out)


def _prune_exports(syms: list[BufferSymbol], defined: set[str]) -> list[BufferSymbol]:
    kept: list[BufferSymbol] = []
    for s in syms:
        if s.kind is SymbolKind.EXPORT and s.name in defined:
            # Redundant declarator: the definition in this file carries the
            # real range/body, so keep only that one.
            continue
        if s.children:
            s = replace(s, children=tuple(_prune_exports(list(s.children), defined)))
        kept.append(s)
    return kept


def _suppress_redundant_exports(syms: list[BufferSymbol]) -> list[BufferSymbol]:
    """Drop EXPORT declarators whose target is defined in the same file.

    ``.export foo`` next to ``.proc foo`` describes one entity, not two.  An
    export with no in-file definition (a re-export of an ``.import``ed symbol,
    or a symbol defined in another translation unit) is left alone, since there
    the declarator is the only thing this file has to say about the name.
    """
    defined: set[str] = set()
    _collect_definition_names(syms, defined)
    if not defined:
        return syms
    return _prune_exports(syms, defined)


def _synthesize_label_bodies(
    syms: list[BufferSymbol],
    src_lines: list[str],
    parent_end: Position | None,
) -> list[BufferSymbol]:
    """Apply the body-range + cheap-local-absorption pass.

    Recurses into container children (``.proc`` etc.) so label-style routines
    embedded inside a ``.scope`` get the same treatment.  ``parent_end`` is the
    end of the enclosing container (``None`` at file level): a label with no
    later sibling boundary stops there instead of running to end-of-file.
    """
    if not syms:
        return syms

    # First recurse so nested label-style routines get fixed too.
    syms = [
        replace(
            s, children=tuple(_synthesize_label_bodies(list(s.children), src_lines, s.range.end))
        )
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
        # the next boundary; else the line before the container's closing
        # directive; else EOF.
        start_line = s.range.start.line
        if nb < n:
            end_line = syms[nb].range.start.line - 1
        elif parent_end is not None:
            end_line = parent_end.line - 1
        else:
            end_line = len(src_lines) - 1
        end_line = max(start_line, end_line)

        end_char = len(src_lines[end_line]) if 0 <= end_line < len(src_lines) else 0
        end = Position(line=end_line, character=end_char)
        if parent_end is not None and (end.line, end.character) > (
            parent_end.line,
            parent_end.character,
        ):
            end = parent_end
        if (end.line, end.character) < (s.range.end.line, s.range.end.character):
            end = s.range.end
        new_range = Range(start=s.range.start, end=end)

        result.append(replace(s, range=new_range, children=tuple(new_children)))

    return result
