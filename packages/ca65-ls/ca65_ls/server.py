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


# Mapping from our CA65-flavored SymbolKind to LSP SymbolKind.
# (LSP doesn't have macro/scope/cheap_local etc., so we approximate.)
_LSP_KIND: dict[SymbolKind, lsp.SymbolKind] = {
    SymbolKind.PROC: lsp.SymbolKind.Function,
    SymbolKind.SCOPE: lsp.SymbolKind.Namespace,
    SymbolKind.MACRO: lsp.SymbolKind.Function,
    SymbolKind.STRUCT: lsp.SymbolKind.Struct,
    SymbolKind.UNION: lsp.SymbolKind.Struct,
    SymbolKind.ENUM: lsp.SymbolKind.Enum,
    SymbolKind.LABEL: lsp.SymbolKind.Variable,
    SymbolKind.CHEAP_LOCAL: lsp.SymbolKind.Variable,
    SymbolKind.ANON_LABEL: lsp.SymbolKind.Variable,
    SymbolKind.CONSTANT: lsp.SymbolKind.Constant,
    SymbolKind.SEGMENT: lsp.SymbolKind.Namespace,
    SymbolKind.IMPORT: lsp.SymbolKind.Variable,
    SymbolKind.EXPORT: lsp.SymbolKind.Variable,
    SymbolKind.FIELD: lsp.SymbolKind.Field,
}


def _to_lsp_range(r: Range) -> lsp.Range:
    return lsp.Range(
        start=lsp.Position(line=r.start.line, character=r.start.character),
        end=lsp.Position(line=r.end.line, character=r.end.character),
    )


def _to_lsp_document_symbol(bs: BufferSymbol) -> lsp.DocumentSymbol:
    return lsp.DocumentSymbol(
        name=bs.name,
        kind=_LSP_KIND.get(bs.kind, lsp.SymbolKind.Variable),
        range=_to_lsp_range(bs.range),
        selection_range=_to_lsp_range(bs.selection_range),
        children=[_to_lsp_document_symbol(c) for c in bs.children] or None,
    )


def _to_lsp_workspace_symbol(ws: WorkspaceSymbol) -> lsp.WorkspaceSymbol:
    return lsp.WorkspaceSymbol(
        name=_qualified_name(ws),
        kind=_LSP_KIND.get(ws.kind, lsp.SymbolKind.Variable),
        location=lsp.Location(uri=ws.uri, range=_to_lsp_range(ws.range)),
    )


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
    doc = ls.doc_or_open(params.text_document.uri)
    return [_to_lsp_document_symbol(s) for s in doc.symbols]


@server.feature(lsp.WORKSPACE_SYMBOL)
def on_workspace_symbol(
    ls: Ca65LanguageServer, params: lsp.WorkspaceSymbolParams
) -> list[lsp.WorkspaceSymbol]:
    out: list[lsp.WorkspaceSymbol] = []
    for idx in ls.indexes.values():
        out.extend(_to_lsp_workspace_symbol(ws) for ws in idx.search(params.query))
    return out


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
    refs = idx.references(name)
    return [lsp.Location(uri=r.uri, range=_to_lsp_range(r.range)) for r in refs]


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
