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
from urllib.parse import unquote, urlparse

import lsprotocol.types as lsp
from pygls.lsp.server import LanguageServer

from ca65_ls import __version__
from ca65_ls.buffer.document import Document
from ca65_ls.index.workspace import WorkspaceIndex
from ca65_ls.types import (
    BufferSymbol,
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
    SymbolKind.LABEL: lsp.SymbolKind.Variable,  # overridden below
    SymbolKind.CHEAP_LOCAL: lsp.SymbolKind.Field,  # distinguishable from siblings
    SymbolKind.ANON_LABEL: lsp.SymbolKind.Variable,
    SymbolKind.CONSTANT: lsp.SymbolKind.Constant,
    SymbolKind.SEGMENT: lsp.SymbolKind.Namespace,
    SymbolKind.IMPORT: lsp.SymbolKind.Interface,  # externally defined
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


def _build_detail(ws: WorkspaceSymbol | None, bs: BufferSymbol | None = None) -> str | None:
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
    bs: BufferSymbol, idx_lookup: dict[tuple[str, tuple[str, ...]], WorkspaceSymbol] | None = None
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


def _dedupe_workspace_symbols(symbols: list[WorkspaceSymbol]) -> list[WorkspaceSymbol]:
    """Drop records that describe the same declaration site twice.

    This used to collapse every (uri, qualified name) pair to one "canonical"
    kind, which hid two things: the `.export` declarator / definition pairs
    (the parser now suppresses those itself, see
    `_suppress_redundant_exports`) and the indexer storing every nested symbol
    two or three times (fixed 2026-09-02 in `WorkspaceIndex`).  It also
    merged genuinely distinct symbols -- two `@loop` cheap locals under
    different parent labels share a qualified name -- so it is now keyed on
    the declaration site and only removes exact repeats.
    """
    seen: set[tuple[str, str, SymbolKind, tuple[str, ...], int, int]] = set()
    out: list[WorkspaceSymbol] = []
    for sym in symbols:
        key = (
            sym.uri,
            sym.name,
            sym.kind,
            sym.scope_path,
            sym.selection_range.start.line,
            sym.selection_range.start.character,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(sym)
    return out


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


# 6502 / 65C02 / 65816 instruction mnemonics.  A cursor on one of these (or
# on a `.directive`) resolves to the operand that follows, so "go to
# definition" on the `jsr` of `jsr foo` lands on `foo`.
_MNEMONICS = frozenset(
    [
        "adc",
        "and",
        "asl",
        "bcc",
        "bcs",
        "beq",
        "bit",
        "bmi",
        "bne",
        "bpl",
        "brk",
        "bvc",
        "bvs",
        "clc",
        "cld",
        "cli",
        "clv",
        "cmp",
        "cpx",
        "cpy",
        "dec",
        "dex",
        "dey",
        "eor",
        "inc",
        "inx",
        "iny",
        "jmp",
        "jsr",
        "lda",
        "ldx",
        "ldy",
        "lsr",
        "nop",
        "ora",
        "pha",
        "php",
        "pla",
        "plp",
        "rol",
        "ror",
        "rti",
        "rts",
        "sbc",
        "sec",
        "sed",
        "sei",
        "sta",
        "stx",
        "sty",
        "tax",
        "tay",
        "tsx",
        "txa",
        "txs",
        "tya",
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
    ]
)


def _identifier_span_at(document_text: str, position: lsp.Position) -> tuple[int, int, str] | None:
    """Locate the CA65 identifier the cursor refers to on its line.

    Returns ``(start, end, name)`` in the line, or None.  When the cursor sits
    on an instruction mnemonic or a `.directive` keyword, the first operand
    identifier after it is returned instead (if there is one), so a request
    anywhere on `jsr foo` / `.proc foo` / `.export foo` is about `foo`.
    """
    lines = document_text.splitlines()
    if position.line >= len(lines):
        return None
    line = lines[position.line]
    if position.character > len(line):
        return None
    matches = list(_IDENT_RE.finditer(line))

    def is_directive(m: re.Match[str]) -> bool:
        return m.start() > 0 and line[m.start() - 1] == "."

    for i, match in enumerate(matches):
        if not (match.start() <= position.character <= match.end()):
            continue
        word = match.group(0)
        if (is_directive(match) or word.lower() in _MNEMONICS) and i + 1 < len(matches):
            operand = matches[i + 1]
            if not is_directive(operand):
                return operand.start(), operand.end(), operand.group(0)
        return match.start(), match.end(), word
    return None


def _identifier_at(document_text: str, position: lsp.Position) -> str | None:
    """Extract the CA65 identifier the cursor refers to, if any."""
    span = _identifier_span_at(document_text, position)
    return span[2] if span else None


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

    def index_for(self, doc_uri: str, *, fresh: bool = False) -> WorkspaceIndex | None:
        """Pick the workspace index whose root is an ancestor of doc_uri.

        With ``fresh=True`` the index is first brought in line with the files
        on disk (throttled to once per `REFRESH_MAX_AGE` seconds), so a query
        sees routines that were created or edited outside the editor since
        the last request.  Serena never sends didSave or file-watcher events,
        so this is what keeps the index current for its symbolic tools.
        """
        try:
            doc_path = _uri_to_path(doc_uri).resolve()
        except Exception:
            return None
        best: tuple[int, WorkspaceIndex] | None = None
        for root in self.workspace_roots:
            try:
                doc_path.relative_to(root.resolve())
            except ValueError:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, self.indexes[_path_to_uri(root)])
        if best is None:
            return None
        if fresh:
            _refresh_quietly(best[1], REFRESH_MAX_AGE)
        return best[1]

    def refresh_all(self, max_age: float | None) -> None:
        for idx in self.indexes.values():
            _refresh_quietly(idx, max_age)

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

#: How old the on-disk view of a workspace may be, in seconds, before a query
#: handler rescans the project for created / modified / deleted files.
REFRESH_MAX_AGE = 1.0

#: Glob patterns registered with clients that support file watching.
WATCH_GLOBS = ("**/*.{s,S,asm,ASM,inc,INC}", "**/*.dbg")


def _refresh_quietly(idx: WorkspaceIndex, max_age: float | None) -> None:
    try:
        idx.refresh(max_age=max_age)
    except Exception as exc:  # a refresh failure must never break a query
        log.warning("index refresh failed for %s: %s", idx.project_root, exc)


def _reindex_quietly(ls: Ca65LanguageServer, uri: str, text: str | None = None) -> None:
    idx = ls.index_for(uri)
    if idx is None:
        return
    try:
        idx.reindex_file(_uri_to_path(uri), text=text)
    except Exception as exc:
        log.warning("reindex_file failed for %s: %s", uri, exc)


def _apply_content_change(text: str, change: object) -> str:
    """Apply one didChange content change to `text`.

    Handles both the whole-document form (`text` only) and the incremental
    form (`range` + `text`), which is what Serena sends for
    `insert_text_at_position` / `delete_text_between_positions`.  pygls keeps
    its own copy of the document too; this is the fallback used when the
    handler is driven without a running protocol (tests).
    """
    rng = getattr(change, "range", None)
    new = getattr(change, "text", "")
    if rng is None:
        return new
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(pos: lsp.Position) -> int:
        if pos.line >= len(lines):
            return len(text)
        return min(offsets[pos.line] + pos.character, offsets[pos.line + 1])

    return text[: offset(rng.start)] + new + text[offset(rng.end) :]


def _document_text_after_change(
    ls: Ca65LanguageServer, doc: Document, params: lsp.DidChangeTextDocumentParams
) -> str:
    """Prefer pygls's own reconciled copy of the document; fall back to
    applying the changes ourselves."""
    try:
        return ls.workspace.get_text_document(params.text_document.uri).source
    except Exception:
        text = doc.text
        for change in params.content_changes:
            text = _apply_content_change(text, change)
        return text


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


def _client_watches_files(capabilities: lsp.ClientCapabilities | None) -> bool:
    ws = getattr(capabilities, "workspace", None)
    watched = getattr(ws, "did_change_watched_files", None)
    return bool(getattr(watched, "dynamic_registration", False))


def _watched_files_registration() -> lsp.RegistrationParams:
    return lsp.RegistrationParams(
        registrations=[
            lsp.Registration(
                id="ca65-ls.watched-files",
                method=lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                register_options=lsp.DidChangeWatchedFilesRegistrationOptions(
                    watchers=[lsp.FileSystemWatcher(glob_pattern=g) for g in WATCH_GLOBS]
                ),
            )
        ]
    )


@server.feature(lsp.INITIALIZED)
def on_initialized(ls: Ca65LanguageServer, params: lsp.InitializedParams) -> None:
    """Ask the client to send `workspace/didChangeWatchedFiles` for sources
    and `.dbg` files.  There is no static server capability for this; it is
    dynamic registration only, so clients without it (Serena) fall back to
    the throttled refresh in `index_for(fresh=True)`."""
    try:
        caps = ls.protocol.client_capabilities  # type: ignore[attr-defined]
    except Exception:
        caps = None
    if not _client_watches_files(caps):
        return
    try:
        ls.client_register_capability(_watched_files_registration())
    except Exception as exc:  # the client may refuse; refresh() still covers us
        log.debug("watched-files registration failed: %s", exc)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def on_did_open(ls: Ca65LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    """The client's buffer is now authoritative for this file.  Also rescan
    the workspace: Serena opens a file before every request, and the file --
    or a sibling it references -- may have been created on disk since the
    last request.  The rescan is throttled like the query handlers', except
    when the opened file itself changed on disk since it was indexed: that
    is the signature of an agent having just written files, so the scan is
    forced (Serena's full-symbol-tree walk opens hundreds of unchanged files
    and must not pay for a git call each time)."""
    uri = params.text_document.uri
    ls.doc_or_open(uri, params.text_document.text)
    idx = ls.index_for(uri)
    if idx is not None:
        try:
            stale = idx.changed_on_disk(_uri_to_path(uri))
        except Exception:
            stale = True
        _refresh_quietly(idx, None if stale else REFRESH_MAX_AGE)
    _reindex_quietly(ls, uri, params.text_document.text)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(ls: Ca65LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    """Update the buffer *and* the workspace index from the new text, so
    definition / references see the edit without a save."""
    uri = params.text_document.uri
    doc = ls.documents.get(uri)
    if doc is None:
        return
    new_text = _document_text_after_change(ls, doc, params)
    doc.update(new_text)
    _reindex_quietly(ls, uri, new_text)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def on_did_close(ls: Ca65LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    """Drop the buffer; the on-disk file is authoritative again."""
    uri = params.text_document.uri
    ls.documents.pop(uri, None)
    _reindex_quietly(ls, uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def on_did_save(ls: Ca65LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    """Refresh the project index for the saved file and recompute diagnostics."""
    uri = params.text_document.uri
    _reindex_quietly(ls, uri)

    diagnostics = _compute_diagnostics(uri)
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


@server.feature(lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def on_did_change_watched_files(
    ls: Ca65LanguageServer, params: lsp.DidChangeWatchedFilesParams
) -> None:
    """Created / changed / deleted files on disk.

    Every workspace that owns one of the events is rescanned: new sources are
    indexed, modified ones reparsed, deleted ones lose their symbols and
    references, and a changed `.dbg` reloads the linker debug info so
    addresses stay current (Serena review F05).  Going through `refresh()`
    rather than reindexing the named file keeps the ignore rules (gitignore,
    tool directories) authoritative, and a file open in an editor buffer
    keeps the buffer's symbols.
    """
    touched: dict[int, WorkspaceIndex] = {}
    for event in params.changes:
        idx = ls.index_for(event.uri)
        if idx is not None:
            touched[id(idx)] = idx
    for idx in touched.values():
        _refresh_quietly(idx, None)


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def on_document_symbol(
    ls: Ca65LanguageServer, params: lsp.DocumentSymbolParams
) -> list[lsp.DocumentSymbol]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    idx = ls.index_for(uri, fresh=True)
    idx_lookup: dict[tuple[str, tuple[str, ...]], WorkspaceSymbol] | None = None
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
    ls.refresh_all(REFRESH_MAX_AGE)
    raw: list[WorkspaceSymbol] = []
    for idx in ls.indexes.values():
        raw.extend(idx.search(params.query))
    deduped = _dedupe_workspace_symbols(raw)
    return [_to_lsp_workspace_symbol(ws) for ws in deduped]


_IMPLEMENTATION_KINDS = frozenset(
    {
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.MACRO,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.ENUM,
    }
)
_LABEL_KINDS = frozenset({SymbolKind.LABEL, SymbolKind.CHEAP_LOCAL, SymbolKind.CONSTANT})
_DECLARATOR_KINDS = frozenset({SymbolKind.IMPORT, SymbolKind.EXPORT})


def _by_kind_preference(candidates: list[WorkspaceSymbol]) -> list[WorkspaceSymbol]:
    """Implementation kinds, then defining labels, then declarators; stable
    within a tier."""
    return (
        [ws for ws in candidates if ws.kind in _IMPLEMENTATION_KINDS]
        or [ws for ws in candidates if ws.kind in _LABEL_KINDS]
        or candidates
    )


def _definition_candidates(
    idx: WorkspaceIndex, uri: str, doc: Document, position: lsp.Position, name: str
) -> list[WorkspaceSymbol]:
    """Rank the definitions of `name` as seen from `position` in `uri`.

    Tiers, first non-empty wins:

    1. definitions in this file lying strictly inside the enclosing routine
       (proc-local labels, cheap locals) -- a `done:` inside `.proc b` never
       resolves to another proc's `done:`;
    2. this file's own definitions.  A label the calling file defines itself
       beats a `.proc` of the same name in an unrelated file
       (c64-wireguard `print_string`);
    3. when this file `.import`s the name, definitions in files that
       `.export` it;
    4. everything else, implementation kinds before labels before
       declarators.
    """
    candidates = idx.lookup(name)
    if not candidates:
        return []

    enclosing = _enclosing_routine(doc, position, exclude_name=name)
    if enclosing is not None:
        local = [
            ws
            for ws in candidates
            if ws.uri == uri and _range_strictly_inside(ws.range, enclosing.range)
        ]
        if local:
            return _by_kind_preference(local)

    own = [ws for ws in candidates if ws.uri == uri and ws.kind not in _DECLARATOR_KINDS]
    if own:
        return _by_kind_preference(own)

    imported_here = any(ws.uri == uri and ws.kind == SymbolKind.IMPORT for ws in candidates)
    if imported_here:
        exporters = [
            ws
            for ws in candidates
            if ws.uri != uri and ws.kind not in _DECLARATOR_KINDS and name in idx.exports_of(ws.uri)
        ]
        if exporters:
            return _by_proximity(exporters, uri)

    # A declarator is never the answer when a real definition is known: a
    # `jmp ip65_init` in a stub file must land on the routine, not on the
    # file's own `.import ip65_init` (adversarial review, 2026-09-02).
    defined = [ws for ws in candidates if ws.kind not in _DECLARATOR_KINDS]
    return _by_proximity(defined or candidates, uri)


def _shared_prefix_depth(a: str, b: str) -> int:
    """Number of leading path segments two URIs have in common."""
    pa, pb = a.split("/"), b.split("/")
    n = 0
    for x, y in zip(pa[:-1], pb[:-1], strict=False):
        if x != y:
            break
        n += 1
    return n


def _by_proximity(candidates: list[WorkspaceSymbol], uri: str) -> list[WorkspaceSymbol]:
    """Nearest definition first, by shared directory prefix with the caller.

    A project can legitimately hold two definitions of a name: c64-wireguard
    has its own `src/crypto/fe25519.s` *and* the vendored submodule copy in
    `libs/x25519/src/fe25519.s`.  A call in `src/crypto/x25519.s` means the
    sibling in its own directory, not the vendored one, even though the
    vendored one is a `.proc` and the sibling only a label -- so proximity
    outranks kind here (adversarial review, 2026-09-02).
    """
    best = max((_shared_prefix_depth(ws.uri, uri) for ws in candidates), default=0)
    nearest = [ws for ws in candidates if _shared_prefix_depth(ws.uri, uri) == best]
    # Proximity first, kind only within the nearest group: a sibling label
    # must not lose to a `.proc` in a vendored copy.
    return _by_kind_preference(nearest or candidates)


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def on_definition(ls: Ca65LanguageServer, params: lsp.DefinitionParams) -> list[lsp.Location]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return []
    idx = ls.index_for(uri, fresh=True)
    if idx is None:
        return []
    defs = _definition_candidates(idx, uri, doc, params.position, name)
    return [lsp.Location(uri=ws.uri, range=_to_lsp_range(ws.range)) for ws in defs]


_SCOPE_CONTAINER_KINDS = frozenset(
    {
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.LABEL,  # only counted as a container when range spans >1 line
    }
)


def _enclosing_routine(
    doc, position: lsp.Position, *, exclude_name: str | None = None
) -> BufferSymbol | None:
    """Return the *smallest* routine-like symbol whose body range contains the
    LSP `position`, or None if the position is at file scope.

    "Routine-like" = ``.proc`` / ``.scope`` / a ``label:`` that gained a
    multi-line body during the post-process pass.

    `exclude_name` is the identifier being queried.  With the cursor on the
    definition `done:` of a proc-local label, the label's own body is the
    smallest container, and "is `done` strictly inside its container" would
    compare the label with itself and answer no -- which sent references and
    rename for every proc-local label project-wide.  Skipping the queried
    name yields the proc around it instead.
    """
    best = None
    best_span = None
    for s in doc.flat_symbols():
        if s.kind not in _SCOPE_CONTAINER_KINDS:
            continue
        if exclude_name is not None and s.name == exclude_name:
            continue
        # Skip single-line LABELs — they're data labels, not routines.
        if s.kind == SymbolKind.LABEL and s.range.end.line <= s.range.start.line:
            continue
        # Position is inside [start, end] line range (inclusive on end line).
        if not (s.range.start.line <= position.line <= s.range.end.line):
            continue
        span = (
            s.range.end.line - s.range.start.line,
            s.range.end.character - s.range.start.character,
        )
        if best is None or span < best_span:
            best, best_span = s, span
    return best


def _range_strictly_inside(inner: Range, outer: Range) -> bool:
    """True iff inner is fully contained in outer AND not equal."""
    if (inner.start.line, inner.start.character) < (outer.start.line, outer.start.character):
        return False
    if (inner.end.line, inner.end.character) > (outer.end.line, outer.end.character):
        return False
    return inner != outer


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
def on_hover(ls: Ca65LanguageServer, params: lsp.HoverParams) -> lsp.Hover | None:
    """Return a hover panel for the identifier under the cursor.

    Resolution matches `on_definition` (`_definition_candidates`): the
    enclosing routine's own labels first, then this file's definitions, then
    exporters of an imported name, then implementation kinds over
    declarators.
    """
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return None
    idx = ls.index_for(uri, fresh=True)
    if idx is None:
        return None

    picked = _definition_candidates(idx, uri, doc, params.position, name)
    if not picked:
        return None
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
def on_references(ls: Ca65LanguageServer, params: lsp.ReferenceParams) -> list[lsp.Location]:
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name:
        return []
    idx = ls.index_for(uri, fresh=True)
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
    enclosing = _enclosing_routine(doc, params.position, exclude_name=name)
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


# ----------------------------------------------------------- rename utilities


def _is_renameable(name: str) -> bool:
    """We refuse to rename anonymous labels (`:` / `:+` / `:-`) because their
    identity is positional, not nominal — there's no name to replace."""
    return bool(name) and name != ":" and not name.startswith(":")


def _compute_rename_scope(
    ls: Ca65LanguageServer, uri: str, doc, name: str, position: lsp.Position
) -> tuple[str, Range] | None:
    """Mirror the scope-aware logic of `on_references` for rename: cheap
    locals + scope-local labels stay confined to their enclosing routine,
    top-level names get a global rename.  Returns the body_filter tuple or
    None if global."""
    idx = ls.index_for(uri)
    if idx is None:
        return None
    enclosing = _enclosing_routine(doc, position, exclude_name=name)
    if enclosing is None:
        return None
    if name.startswith("@"):
        return (uri, enclosing.range)
    for ws in idx.lookup(name):
        if ws.uri == uri and _range_strictly_inside(ws.range, enclosing.range):
            return (uri, enclosing.range)
    return None


@server.feature(lsp.TEXT_DOCUMENT_PREPARE_RENAME)
def on_prepare_rename(ls: Ca65LanguageServer, params: lsp.PrepareRenameParams):
    """Tell the client up-front whether the symbol at the cursor can be
    renamed, and which range the new name will replace."""
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    span = _identifier_span_at(doc.text, params.position)
    if span is None or not _is_renameable(span[2]):
        return None
    # The exact range of the identifier the rename is about -- what the
    # client offers the user as the "old name" they're editing.  Same
    # resolution as on_rename, so a cursor on `jsr` renames the callee.
    start, end, name = span
    return lsp.PrepareRenamePlaceholder(
        range=lsp.Range(
            start=lsp.Position(line=params.position.line, character=start),
            end=lsp.Position(line=params.position.line, character=end),
        ),
        placeholder=name,
    )


@server.feature(lsp.TEXT_DOCUMENT_RENAME)
def on_rename(ls: Ca65LanguageServer, params: lsp.RenameParams) -> lsp.WorkspaceEdit | None:
    """Rename a symbol everywhere it's referenced.

    Scope rules match on_references:
      - Cheap locals (`@name`): rename within the enclosing routine's body
        only.  If the new name lacks the `@`, we add it automatically so the
        user can type just `loop2` and get `@loop2`.
      - Labels defined inside a `.proc`/`.scope` (scope_path non-empty) or
        whose body lives strictly inside an enclosing routine: same as cheap
        locals — scope-confined rename.
      - Top-level labels / .proc / .scope / .macro / .struct names: global
        rename across the workspace, hitting the definition, every .import
        and .export declarator, and every call site.
      - Anonymous labels (`:`) — refused via on_prepare_rename.
    """
    uri = params.text_document.uri
    doc = ls.doc_or_open(uri)
    name = _identifier_at(doc.text, params.position)
    if not name or not _is_renameable(name):
        return None
    new_name = params.new_name
    # Preserve the @ prefix for cheap-local renames; the user can type either form.
    if name.startswith("@") and not new_name.startswith("@"):
        new_name = "@" + new_name
    if not name.startswith("@") and new_name.startswith("@"):
        # Adding a @ to a non-cheap-local rename would change its kind; refuse.
        return None

    idx = ls.index_for(uri, fresh=True)
    if idx is None:
        return None

    body_filter = _compute_rename_scope(ls, uri, doc, name, params.position)

    # Collect every site to edit: definition selection_ranges + references.
    edits_by_uri: dict[str, list[lsp.TextEdit]] = {}

    def _add(uri_: str, rng: Range) -> None:
        edits_by_uri.setdefault(uri_, []).append(
            lsp.TextEdit(range=_to_lsp_range(rng), new_text=new_name)
        )

    for ws in idx.lookup(name):
        # Apply scope filter to definitions, same as references.
        if body_filter is not None:
            if ws.uri != body_filter[0]:
                continue
            if not _position_in_range_lsp(ws.range.start, body_filter[1]):
                continue
        _add(ws.uri, ws.selection_range)

    for ref in idx.references(name, body_filter=body_filter):
        _add(ref.uri, ref.range)

    if not edits_by_uri:
        return None

    return lsp.WorkspaceEdit(changes=edits_by_uri)


def _position_in_range_lsp(pos, r: Range) -> bool:
    """Same as the index's _position_in_range, but operates on the (BufferSymbol)
    Position / Range types directly so we can call it from server-layer code."""
    if (pos.line, pos.character) < (r.start.line, r.start.character):
        return False
    return not (pos.line, pos.character) >= (r.end.line, r.end.character)


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


#: Loggers that echo every JSON-RPC payload ("Sending data: ...", "Received
#: ...") at INFO.  Serena re-logs any stderr line containing "error" as an
#: ERROR, so a payload mentioning `ip65_error` became a spurious error line
#: on every request (Serena review F10).
PROTOCOL_LOGGERS = ("pygls", "lsprotocol")
VERBOSE_ENV = "CA65_LS_VERBOSE"


def configure_logging(level: str = "INFO", *, verbose: bool = False) -> None:
    """Set up stderr logging.  `level` applies to ca65-ls's own logger; the
    pygls / lsprotocol protocol chatter is held at WARNING unless `verbose`
    (the ``--verbose`` flag or ``CA65_LS_VERBOSE=1``) is set, in which case
    everything goes to DEBUG."""
    root_level = logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=root_level, force=True)
    for name in PROTOCOL_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    p = argparse.ArgumentParser(
        prog="ca65-ls",
        description="Language server for CA65 assembly (cc65 toolchain).",
    )
    p.add_argument(
        "--stdio", action="store_true", default=True, help="LSP over stdin/stdout (default)"
    )
    p.add_argument("--log-level", default="INFO", help="DEBUG / INFO / WARNING / ERROR")
    p.add_argument(
        "--verbose",
        action="store_true",
        default=bool(os.environ.get(VERBOSE_ENV)),
        help=f"also log every JSON-RPC payload (or set {VERBOSE_ENV}=1)",
    )
    args = p.parse_args(argv)

    configure_logging(args.log_level, verbose=args.verbose)
    server.start_io()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
