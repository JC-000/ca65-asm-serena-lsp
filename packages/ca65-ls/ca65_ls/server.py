"""
ca65-ls LSP server — pygls 2.x.

The minimum-viable subset that Serena's symbolic agent tools need:

    textDocument/documentSymbol  -> drives get_symbols_overview
    workspace/symbol             -> drives find_symbol
    textDocument/definition      -> drives goto-definition
    textDocument/references      -> drives find_referencing_symbols
    textDocument/publishDiagnostics -> from background ca65 -g

Hover, rename, and completion are M4. semanticTokens is out of scope (Serena
doesn't consume it).

Architecture:
  - One `Document` per open buffer (lazy, evicted on didClose).
  - One `WorkspaceIndex` per workspace folder. Reindex on initialize and on
    file save; incremental update on each didChange via reindex_file().
  - Diagnostics are produced by a synchronous `ca65 -g` invocation on save.
    Real-time diagnostics on every keystroke are too expensive for now.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

import lsprotocol.types as lsp
from pygls.lsp.server import LanguageServer

from ca65_ls import __version__
from ca65_ls.buffer.document import Document
from ca65_ls.index.workspace import WorkspaceIndex
from ca65_ls.types import (
    BufferSymbol,
    Position,
    Range,
    SymbolKind,
    WorkspaceSymbol,
)


log = logging.getLogger("ca65-ls")


# ---------------------------------------------------------------- LSP <-> our types


# Base mapping from our CA65-flavored SymbolKind to LSP SymbolKind.
# Plain LABEL is overridden by `_lsp_kind_of()` below because a routine-like
# label (with absorbed body/children) deserves Function rather than Variable.
_BASE_LSP_KIND: dict[SymbolKind, lsp.SymbolKind] = {
    SymbolKind.PROC: lsp.SymbolKind.Function,
    SymbolKind.SCOPE: lsp.SymbolKind.Namespace,
    SymbolKind.MACRO: lsp.SymbolKind.Function,
    SymbolKind.STRUCT: lsp.SymbolKind.Struct,
    SymbolKind.UNION: lsp.SymbolKind.Struct,
    SymbolKind.ENUM: lsp.SymbolKind.Enum,
    SymbolKind.LABEL: lsp.SymbolKind.Variable,         # overridden below
    SymbolKind.CHEAP_LOCAL: lsp.SymbolKind.Field,      # distinguishable from siblings
    SymbolKind.ANON_LABEL: lsp.SymbolKind.Variable,
    SymbolKind.CONSTANT: lsp.SymbolKind.Constant,
    SymbolKind.SEGMENT: lsp.SymbolKind.Namespace,
    SymbolKind.IMPORT: lsp.SymbolKind.Interface,       # externally defined
    SymbolKind.EXPORT: lsp.SymbolKind.Variable,
    SymbolKind.FIELD: lsp.SymbolKind.Field,
}


def _lsp_kind_of(sym: BufferSymbol | WorkspaceSymbol) -> lsp.SymbolKind:
    """Map our SymbolKind onto an LSP SymbolKind, with one context-sensitive
    promotion: a LABEL that gained a body (multi-line range) or has children is
    a routine; render it as Function. A pure data label stays Variable.
    """
    if sym.kind == SymbolKind.LABEL:
        # WorkspaceSymbol doesn't carry children; BufferSymbol does.  Either a
        # multi-line range or absorbed children is enough signal that this is
        # a routine rather than a single-address data label.
        has_children = bool(getattr(sym, "children", ()))
        has_body = sym.range.end.line > sym.range.start.line
        return lsp.SymbolKind.Function if (has_children or has_body) else lsp.SymbolKind.Variable
    return _BASE_LSP_KIND.get(sym.kind, lsp.SymbolKind.Variable)


def _to_lsp_range(r: Range) -> lsp.Range:
    return lsp.Range(
        start=lsp.Position(line=r.start.line, character=r.start.character),
        end=lsp.Position(line=r.end.line, character=r.end.character),
    )


def _build_detail(ws: Optional[WorkspaceSymbol], bs: Optional[BufferSymbol] = None) -> Optional[str]:
    """Render a short single-line `detail` blurb for a symbol.

    Format examples:
        "$1234 in CODE"
        "$0040 zeropage — 2 bytes"
        "parent: hkdf_extract"          (cheap local without .dbg)
        "scope: helpers::foo"           (scoped label with no other detail)

    Returns None when there's nothing interesting to say.  This goes into LSP
    `DocumentSymbol.detail` / `WorkspaceSymbol.containerName`-adjacent fields
    so MCP clients (Serena, IDE LSPs) can surface it without a separate hover
    round-trip.
    """
    parts: list[str] = []
    if ws is not None:
        if ws.address is not None:
            addr = f"${ws.address:04X}"
            if ws.segment:
                if ws.segment.upper() in ("ZEROPAGE", "ZP"):
                    addr += " zeropage"
                else:
                    addr += f" in {ws.segment}"
            if ws.size is not None:
                addr += f" — {ws.size} byte" + ("s" if ws.size != 1 else "")
            parts.append(addr)
        elif ws.segment:
            parts.append(ws.segment)
    src = ws if ws is not None else bs
    if src is not None:
        if src.parent_label:
            parts.append(f"parent: {src.parent_label}")
        elif src.scope_path:
            parts.append(f"scope: {'::'.join(src.scope_path)}")
    return "; ".join(parts) if parts else None


def _to_lsp_document_symbol(
    bs: BufferSymbol, idx_lookup: Optional[dict[tuple[str, tuple[str, ...]], WorkspaceSymbol]] = None
) -> lsp.DocumentSymbol:
    """Convert one BufferSymbol to LSP DocumentSymbol.  When `idx_lookup` is
    supplied, enrich `detail` with workspace-index info (address/segment)."""
    ws = None
    if idx_lookup is not None:
        ws = idx_lookup.get((bs.name, bs.scope_path))
    return lsp.DocumentSymbol(
        name=bs.name,
        kind=_lsp_kind_of(bs),
        detail=_build_detail(ws, bs),
        range=_to_lsp_range(bs.range),
        selection_range=_to_lsp_range(bs.selection_range),
        children=[_to_lsp_document_symbol(c, idx_lookup) for c in bs.children] or None,
    )


def _to_lsp_workspace_symbol(ws: WorkspaceSymbol) -> lsp.WorkspaceSymbol:
    detail = _build_detail(ws)
    name = _qualified_name(ws)
    if detail:
        # WorkspaceSymbol has no `detail` field in the LSP protocol; surface
        # the info via `containerName`, which IDEs render alongside the name.
        return lsp.WorkspaceSymbol(
            name=name,
            kind=_lsp_kind_of(ws),
            location=lsp.Location(uri=ws.uri, range=_to_lsp_range(ws.range)),
            container_name=detail,
        )
    return lsp.WorkspaceSymbol(
        name=name,
        kind=_lsp_kind_of(ws),
        location=lsp.Location(uri=ws.uri, range=_to_lsp_range(ws.range)),
    )


# Symbol kinds that we treat as the "canonical definition" when deduplicating
# workspace_symbol results — see _dedupe_workspace_symbols.
_CANONICAL_DEF_KINDS = (
    SymbolKind.PROC,
    SymbolKind.SCOPE,
    SymbolKind.MACRO,
    SymbolKind.STRUCT,
    SymbolKind.UNION,
    SymbolKind.ENUM,
    SymbolKind.LABEL,
    SymbolKind.CONSTANT,
    SymbolKind.FIELD,
    SymbolKind.CHEAP_LOCAL,
    SymbolKind.ANON_LABEL,
    SymbolKind.EXPORT,   # .export name — the declarator IS the def if there's no body
    SymbolKind.IMPORT,   # .import — last resort, externally defined
)


def _dedupe_workspace_symbols(symbols: list[WorkspaceSymbol]) -> list[WorkspaceSymbol]:
    """Collapse the (declarator, definition) pairs that ca65 emits.

    For every `.export hkdf_extract` declarator + `hkdf_extract:` label, we
    return only the canonical definition.  Same name in two different scopes
    or two different files stays as two entries — we key dedup on
    (uri, qualified_name) so different files keep distinct hits.

    Ranking: pick the entry whose kind appears earliest in
    _CANONICAL_DEF_KINDS.  Ties broken by first-seen.
    """
    rank = {k: i for i, k in enumerate(_CANONICAL_DEF_KINDS)}
    best: dict[tuple[str, str], WorkspaceSymbol] = {}
    order: list[tuple[str, str]] = []
    for sym in symbols:
        key = (sym.uri, _qualified_name(sym))
        prev = best.get(key)
        if prev is None:
            best[key] = sym
            order.append(key)
        else:
            if rank.get(sym.kind, len(rank)) < rank.get(prev.kind, len(rank)):
                best[key] = sym
    return [best[k] for k in order]


def _qualified_name(ws: WorkspaceSymbol) -> str:
    """Render `("helpers", "foo")` + name="bar" as "helpers::foo::bar"."""
    if not ws.scope_path:
        return ws.name
    return "::".join((*ws.scope_path, ws.name))


def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


def _path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


# CA65 identifier regex: letters/underscore, then alnum/underscore.
# Cheap locals are `@name`; anonymous labels are bare `:` (not handled here).
_IDENT_RE = re.compile(r"@?[A-Za-z_][A-Za-z0-9_]*")


def _identifier_at(document_text: str, position: lsp.Position) -> Optional[str]:
    """Extract the CA65 identifier under (line, character), if any."""
    lines = document_text.splitlines()
    if position.line >= len(lines):
        return None
    line = lines[position.line]
    if position.character > len(line):
        return None
    for match in _IDENT_RE.finditer(line):
        if match.start() <= position.character <= match.end():
            return match.group(0)
    return None


# -------------------------------------------------------------------- server class


class Ca65LanguageServer(LanguageServer):
    """pygls 2.x `LanguageServer` plus our per-session state."""

    def __init__(self) -> None:
        super().__init__(name="ca65-ls", version=__version__)
        self.documents: dict[str, Document] = {}
        self.indexes: dict[str, WorkspaceIndex] = {}  # workspace-root URI -> index
        self.workspace_roots: list[Path] = []

    # ---- workspace bookkeeping ------------------------------------------------

    def add_workspace(self, root: Path) -> WorkspaceIndex:
        uri = _path_to_uri(root)
        if uri not in self.indexes:
            idx = WorkspaceIndex(root)
            idx.reindex()
            self.indexes[uri] = idx
            self.workspace_roots.append(root)
        return self.indexes[uri]

    def index_for(self, doc_uri: str) -> Optional[WorkspaceIndex]:
        """Pick the workspace index whose root is an ancestor of doc_uri."""
        try:
            doc_path = _uri_to_path(doc_uri).resolve()
        except Exception:
            return None
        best: Optional[tuple[int, WorkspaceIndex]] = None
        for root in self.workspace_roots:
            try:
                doc_path.relative_to(root.resolve())
            except ValueError:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, self.indexes[_path_to_uri(root)])
        return best[1] if best else None

    def doc_or_open(self, uri: str, text: str | None = None) -> Document:
        doc = self.documents.get(uri)
        if doc is not None:
            if text is not None and doc.text != text:
                doc.update(text)
            return doc
        if text is None:
            text = _uri_to_path(uri).read_text(encoding="utf-8")
        doc = Document(uri, text)
        self.documents[uri] = doc
        return doc


server = Ca65LanguageServer()


# -------------------------------------------------------------------- handlers


@server.feature(lsp.INITIALIZE)
def on_initialize(ls: Ca65LanguageServer, params: lsp.InitializeParams) -> None:
    """Spin up a WorkspaceIndex for each workspace folder the client gave us."""
    roots: list[Path] = []
    if params.workspace_folders:
        for folder in params.workspace_folders:
            roots.append(_uri_to_path(folder.uri))
    elif params.root_uri:
        roots.append(_uri_to_path(params.root_uri))
    elif params.root_path:
        roots.append(Path(params.root_path))

    for root in roots:
        try:
            ls.add_workspace(root)
        except Exception as exc:
            log.warning("Failed to index workspace %s: %s", root, exc)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def on_did_open(ls: Ca65LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    ls.doc_or_open(params.text_document.uri, params.text_document.text)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(ls: Ca65LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    doc = ls.documents.get(uri)
    if doc is None:
        return
    # We register for full-document sync (see capabilities below), so each
    # change carries the full new text.
    new_text = params.content_changes[-1].text  # type: ignore[union-attr]
    doc.update(new_text)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def on_did_close(ls: Ca65LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    ls.documents.pop(params.text_document.uri, None)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def on_did_save(ls: Ca65LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    """Refresh the project index for the saved file and recompute diagnostics."""
    uri = params.text_document.uri
    idx = ls.index_for(uri)
    if idx is not None:
        try:
            idx.reindex_file(_uri_to_path(uri))
        except Exception as exc:
            log.warning("reindex_file failed for %s: %s", uri, exc)

    diagnostics = _compute_diagnostics(uri)
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def on_document_symbol(
    ls: Ca65LanguageServer, params: lsp.DocumentSymbolParams
) -> list[lsp.DocumentSymbol]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    idx = ls.index_for(uri)
    idx_lookup: Optional[dict[tuple[str, tuple[str, ...]], WorkspaceSymbol]] = None
    if idx is not None:
        # Build a quick (name, scope_path) -> WorkspaceSymbol lookup for THIS
        # file, so we can attach detail (address/segment/size) without doing
        # idx.lookup() per symbol.
        idx_lookup = {}
        for ws in idx.all_symbols():
            if ws.uri == uri:
                idx_lookup[(ws.name, ws.scope_path)] = ws
    return [_to_lsp_document_symbol(s, idx_lookup) for s in doc.symbols]


@server.feature(lsp.WORKSPACE_SYMBOL)
def on_workspace_symbol(
    ls: Ca65LanguageServer, params: lsp.WorkspaceSymbolParams
) -> list[lsp.WorkspaceSymbol]:
    # Gather all hits, then collapse (declarator, definition) pairs so the
    # client doesn't see two entries for every `.export name` / `name:` pair.
    raw: list[WorkspaceSymbol] = []
    for idx in ls.indexes.values():
        raw.extend(idx.search(params.query))
    deduped = _dedupe_workspace_symbols(raw)
    return [_to_lsp_workspace_symbol(ws) for ws in deduped]


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def on_definition(
    ls: Ca65LanguageServer, params: lsp.DefinitionParams
) -> list[lsp.Location]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return []
    idx = ls.index_for(uri)
    if idx is None:
        return []
    # Of all candidates with this name, prefer the implementation over any
    # declarators. Order of preference:
    #   1. PROC / SCOPE / MACRO / STRUCT / UNION / ENUM   (the real thing)
    #   2. LABEL / CHEAP_LOCAL / CONSTANT                 (defining label)
    #   3. EXPORT / IMPORT                                (just a declarator)
    candidates = idx.lookup(name)
    implementation_kinds = {
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.MACRO,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.ENUM,
    }
    label_kinds = {SymbolKind.LABEL, SymbolKind.CHEAP_LOCAL, SymbolKind.CONSTANT}
    defs = (
        [ws for ws in candidates if ws.kind in implementation_kinds]
        or [ws for ws in candidates if ws.kind in label_kinds]
        or candidates
    )
    return [lsp.Location(uri=ws.uri, range=_to_lsp_range(ws.range)) for ws in defs]


_SCOPE_CONTAINER_KINDS = frozenset({
    SymbolKind.PROC,
    SymbolKind.SCOPE,
    SymbolKind.LABEL,   # only counted as a container when range spans >1 line
})


def _enclosing_routine(doc, position: lsp.Position) -> "BufferSymbol | None":
    """Return the *smallest* routine-like symbol whose body range contains the
    LSP `position`, or None if the position is at file scope.

    "Routine-like" = ``.proc`` / ``.scope`` / a ``label:`` that gained a
    multi-line body during the post-process pass.
    """
    best = None
    best_span = None
    for s in doc.flat_symbols():
        if s.kind not in _SCOPE_CONTAINER_KINDS:
            continue
        # Skip single-line LABELs — they're data labels, not routines.
        if s.kind == SymbolKind.LABEL and s.range.end.line <= s.range.start.line:
            continue
        # Position is inside [start, end] line range (inclusive on end line).
        if not (s.range.start.line <= position.line <= s.range.end.line):
            continue
        span = (s.range.end.line - s.range.start.line, s.range.end.character - s.range.start.character)
        if best is None or span < best_span:
            best, best_span = s, span
    return best


def _range_strictly_inside(inner: Range, outer: Range) -> bool:
    """True iff inner is fully contained in outer AND not equal."""
    if (inner.start.line, inner.start.character) < (outer.start.line, outer.start.character):
        return False
    if (inner.end.line, inner.end.character) > (outer.end.line, outer.end.character):
        return False
    if inner == outer:
        return False
    return True


# --------------------------------------------------------------- hover utilities


def _doc_comment_above(text: str, line_no: int) -> str:
    """Walk backwards from `line_no` collecting consecutive comment lines.

    A "comment line" starts (after leading whitespace) with `;`.  Returns the
    contiguous block ABOVE `line_no` with leading `;` and one optional space
    stripped, in source order.  Empty string if there's nothing.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = line_no - 1
    while i >= 0:
        stripped = lines[i].lstrip()
        if not stripped.startswith(";"):
            break
        # strip ';' and one space; preserve internal formatting
        cleaned = stripped[1:]
        if cleaned.startswith(" "):
            cleaned = cleaned[1:]
        out.append(cleaned)
        i -= 1
    return "\n".join(reversed(out))


_HOVER_KIND_LABEL: dict[SymbolKind, str] = {
    SymbolKind.PROC: "procedure",
    SymbolKind.SCOPE: "scope",
    SymbolKind.MACRO: "macro",
    SymbolKind.STRUCT: "struct",
    SymbolKind.UNION: "union",
    SymbolKind.ENUM: "enum",
    SymbolKind.LABEL: "label",
    SymbolKind.CHEAP_LOCAL: "cheap local",
    SymbolKind.ANON_LABEL: "anonymous label",
    SymbolKind.CONSTANT: "constant",
    SymbolKind.SEGMENT: "segment",
    SymbolKind.IMPORT: "imported symbol",
    SymbolKind.EXPORT: "exported symbol",
    SymbolKind.FIELD: "field",
}


def _build_hover_markdown(ws: WorkspaceSymbol, doc_text: str | None) -> str:
    """Render a WorkspaceSymbol as a markdown hover panel.

    Layout:
      **name**  _(kind)_
      <scope/parent lines>
      <address/segment/size when .dbg-enriched>
      ---
      <doc-comment block above the definition>
    """
    kind_label = _HOVER_KIND_LABEL.get(ws.kind, ws.kind.value)
    lines = [f"**{ws.name}**  _{kind_label}_"]

    if ws.scope_path:
        lines.append(f"scope: `{'::'.join(ws.scope_path)}`")
    if ws.parent_label:
        lines.append(f"parent: `{ws.parent_label}`")

    if ws.address is not None:
        addr_str = f"${ws.address:04X}"
        if ws.segment:
            addr_str += f" in `{ws.segment}`"
        if ws.size is not None:
            addr_str += f" — {ws.size} byte" + ("s" if ws.size != 1 else "")
        lines.append(addr_str)
    elif ws.segment:
        lines.append(f"segment: `{ws.segment}`")

    if doc_text is not None:
        comment = _doc_comment_above(doc_text, ws.range.start.line)
        if comment:
            lines.append("")
            lines.append("---")
            lines.append("```ca65")
            lines.append(comment)
            lines.append("```")

    return "\n\n".join(lines)


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def on_hover(
    ls: Ca65LanguageServer, params: lsp.HoverParams
) -> Optional[lsp.Hover]:
    """Return a hover panel for the identifier under the cursor.

    Resolution preference matches `on_definition`: implementation kinds beat
    declarators.  For cheap locals (`@name`), prefer the candidate whose
    parent_label matches the cursor's enclosing routine — otherwise the
    workspace might surface a same-named cheap local from another routine.
    """
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return None
    idx = ls.index_for(uri)
    if idx is None:
        return None

    candidates = idx.lookup(name)
    if not candidates:
        return None

    # If the cursor is inside a routine and the symbol is a cheap local, pick
    # the candidate whose parent_label matches that routine.
    enclosing = _enclosing_routine(doc, params.position)
    if enclosing is not None and name.startswith("@"):
        scoped = [c for c in candidates if c.parent_label == enclosing.name]
        if scoped:
            candidates = scoped

    # Otherwise pick the canonical definition (matches on_definition's logic).
    implementation_kinds = {
        SymbolKind.PROC, SymbolKind.SCOPE, SymbolKind.MACRO,
        SymbolKind.STRUCT, SymbolKind.UNION, SymbolKind.ENUM,
    }
    label_kinds = {SymbolKind.LABEL, SymbolKind.CHEAP_LOCAL, SymbolKind.CONSTANT}
    picked = (
        [c for c in candidates if c.kind in implementation_kinds]
        or [c for c in candidates if c.kind in label_kinds]
        or candidates
    )
    ws = picked[0]

    # Load the defining file's text for the comment-above lookup. If the
    # symbol is defined in the open buffer, reuse that to honor unsaved edits.
    if ws.uri == uri:
        def_text: str | None = doc.text
    else:
        try:
            def_text = _uri_to_path(ws.uri).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            def_text = None

    contents = lsp.MarkupContent(
        kind=lsp.MarkupKind.Markdown,
        value=_build_hover_markdown(ws, def_text),
    )
    return lsp.Hover(contents=contents, range=_to_lsp_range(ws.range))


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
def on_references(
    ls: Ca65LanguageServer, params: lsp.ReferenceParams
) -> list[lsp.Location]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return []
    idx = ls.index_for(uri)
    if idx is None:
        return []

    # Scope-aware filtering:
    #   - Cheap locals (`@name`) are always scoped to their parent label's
    #     body.  Many parents may have an `@loop`; the cursor disambiguates.
    #   - Plain labels are scoped only if the workspace index says this
    #     particular name is defined *inside* the enclosing routine (i.e. it
    #     is a routine-internal jump target, not a global routine entry).
    #     A top-level routine name (e.g. `hkdf_extract`) is the enclosing
    #     routine itself and stays global.
    body_filter: tuple[str, Range] | None = None
    enclosing = _enclosing_routine(doc, params.position)
    if enclosing is not None:
        if name.startswith("@"):
            body_filter = (uri, _r_to_internal(enclosing.range))
        else:
            for ws in idx.lookup(name):
                if ws.uri == uri and _range_strictly_inside(ws.range, enclosing.range):
                    body_filter = (uri, _r_to_internal(enclosing.range))
                    break

    refs = idx.references(name, body_filter=body_filter)
    return [lsp.Location(uri=r.uri, range=_to_lsp_range(r.range)) for r in refs]


def _r_to_internal(r: Range) -> Range:
    """Identity for now; placeholder for any future BufferSymbol-Range vs
    LSP-Range conversion. Keeps `on_references` readable."""
    return r


# -------------------------------------------------------------------- diagnostics


_CA65_ERR_RE = re.compile(
    r"^(?P<file>[^:(]+?)[(:](?P<line>\d+)[):]\s*(?P<severity>Warning|Error):\s*(?P<msg>.*)$"
)


def _compute_diagnostics(uri: str) -> list[lsp.Diagnostic]:
    """Run `ca65 -g` on the saved file and parse its stderr."""
    path = _uri_to_path(uri)
    if not path.exists():
        return []
    # We deliberately do not link — ca65 alone catches syntax errors and
    # branch-out-of-range. Linker errors (undefined symbol etc.) are M4.
    include_dirs: list[str] = []
    inc_dir = path.parent.parent / "inc"
    if inc_dir.is_dir():
        include_dirs.extend(["-I", str(inc_dir)])
    try:
        proc = subprocess.run(
            ["ca65", "-g", *include_dirs, "-o", "/dev/null", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("ca65 invocation failed for %s: %s", path, exc)
        return []

    diagnostics: list[lsp.Diagnostic] = []
    for line in proc.stderr.splitlines():
        m = _CA65_ERR_RE.match(line)
        if not m:
            continue
        lineno = max(0, int(m.group("line")) - 1)
        severity = (
            lsp.DiagnosticSeverity.Error
            if m.group("severity") == "Error"
            else lsp.DiagnosticSeverity.Warning
        )
        diagnostics.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=lineno, character=0),
                    end=lsp.Position(line=lineno, character=1024),
                ),
                severity=severity,
                source="ca65",
                message=m.group("msg"),
            )
        )
    return diagnostics


# -------------------------------------------------------------------- entry point


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="ca65-ls",
        description="Language server for CA65 assembly (cc65 toolchain).",
    )
    p.add_argument("--stdio", action="store_true", default=True, help="LSP over stdin/stdout (default)")
    p.add_argument("--log-level", default="INFO", help="DEBUG / INFO / WARNING / ERROR")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    server.start_io()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
