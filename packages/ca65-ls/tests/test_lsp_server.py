"""
LSP server integration tests.

We don't spin up a real JSON-RPC client/server pair — pygls's TestClient is
heavy and asyncio-flavored. Instead we call the registered feature handlers
directly: they are pure functions of (LanguageServer, params), which is exactly
what pygls dispatches.
"""

from __future__ import annotations

from pathlib import Path

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.server import Ca65LanguageServer, _identifier_at, _qualified_name

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "test_repo"
DBG_PATH = FIXTURE_DIR / "build" / "test_repo.dbg"


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def ls(tmp_path):
    """A fresh Ca65LanguageServer bound to a copy of the synthetic corpus.

    We copy the fixtures into tmp_path so cache directories the index creates
    don't pollute the committed test fixtures.
    """
    import shutil

    dst = tmp_path / "test_repo"
    shutil.copytree(FIXTURE_DIR, dst)
    server = Ca65LanguageServer()
    server.add_workspace(dst)
    yield server, dst


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


def _open_file(server: Ca65LanguageServer, path: Path) -> str:
    """Helper: have the server load a file via the didOpen path."""
    uri = _uri(path)
    text = path.read_text()
    srv.on_did_open(
        server,
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(uri=uri, language_id="ca65", version=1, text=text)
        ),
    )
    return uri


# ----------------------------------------------------------- identifier_at unit


def test_identifier_at_basic():
    text = "        jsr     lib_export\n"
    # Position 0 chars in: whitespace, no identifier
    assert _identifier_at(text, lsp.Position(line=0, character=0)) is None
    # Mid-identifier
    assert _identifier_at(text, lsp.Position(line=0, character=20)) == "lib_export"


def test_identifier_at_cheap_local():
    text = "@retry: lda foo\n"
    assert _identifier_at(text, lsp.Position(line=0, character=2)) == "@retry"


# ------------------------------------------------------------- documentSymbol


def test_document_symbol_helpers_hierarchy(ls):
    server, root = ls
    uri = _open_file(server, root / "src" / "helpers.s")
    result = srv.on_document_symbol(
        server, lsp.DocumentSymbolParams(text_document=lsp.TextDocumentIdentifier(uri=uri))
    )
    # We expect to see at least .struct S, .macro mac1, and .scope helpers at top level.
    top_level_names = [s.name for s in result]
    assert "S" in top_level_names
    assert "mac1" in top_level_names
    assert "helpers" in top_level_names

    # Find the helpers scope and assert its children include foo and bar.
    helpers = next(s for s in result if s.name == "helpers")
    assert helpers.kind == lsp.SymbolKind.Namespace
    child_names = [c.name for c in (helpers.children or [])]
    assert "foo" in child_names
    assert "bar" in child_names


def test_document_symbol_main_includes_start_proc(ls):
    server, root = ls
    uri = _open_file(server, root / "src" / "main.s")
    result = srv.on_document_symbol(
        server, lsp.DocumentSymbolParams(text_document=lsp.TextDocumentIdentifier(uri=uri))
    )
    names = [s.name for s in result]
    assert "_start" in names


# ------------------------------------------------------------- workspace/symbol


def test_workspace_symbol_finds_helpers_foo(ls):
    server, root = ls
    result = srv.on_workspace_symbol(server, lsp.WorkspaceSymbolParams(query="helpers_foo"))
    names = [ws.name for ws in result]
    assert "helpers_foo" in names


def test_workspace_symbol_substring(ls):
    server, root = ls
    result = srv.on_workspace_symbol(server, lsp.WorkspaceSymbolParams(query="lib"))
    names = [ws.name for ws in result]
    assert any("lib_export" in n for n in names)
    assert any("lib_buffer" in n for n in names)


def test_workspace_symbol_qualifies_scoped_names(ls):
    """Symbols inside `.scope helpers` should render as `helpers::foo` for the client."""
    server, root = ls
    result = srv.on_workspace_symbol(server, lsp.WorkspaceSymbolParams(query="foo"))
    qualified = [ws.name for ws in result]
    assert any(n == "helpers::foo" for n in qualified), f"got {qualified}"


# ------------------------------------------------------------- definition


def _position_of(path: Path, line_substr: str, identifier: str) -> lsp.Position:
    """Find the (line, column) of `identifier` on the line that contains `line_substr`."""
    for lineno, line in enumerate(path.read_text().splitlines()):
        if line_substr in line:
            col = line.index(identifier)
            return lsp.Position(line=lineno, character=col + 1)
    raise ValueError(f"{line_substr!r} not found in {path}")


def test_definition_jumps_to_lib_export(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open_file(server, main)
    # In main.s find the `jsr lib_export` line; jump to definition.
    pos = _position_of(main, "jsr     lib_export", "lib_export")
    result = srv.on_definition(
        server,
        lsp.DefinitionParams(text_document=lsp.TextDocumentIdentifier(uri=uri), position=pos),
    )
    assert len(result) == 1
    assert result[0].uri.endswith("src/lib.s")


def test_definition_returns_empty_when_no_identifier(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open_file(server, main)
    # column 0 of an indented line = whitespace, no identifier
    result = srv.on_definition(
        server,
        lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=0, character=0),
        ),
    )
    assert result == []


# ------------------------------------------------------------- references


def test_references_finds_lib_export_use_site(ls):
    server, root = ls
    libs = root / "src" / "lib.s"
    uri = _open_file(server, libs)
    # Place cursor on `lib_export` in lib.s's definition.
    pos = _position_of(libs, ".proc lib_export", "lib_export")
    result = srv.on_references(
        server,
        lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=pos,
            context=lsp.ReferenceContext(include_declaration=True),
        ),
    )
    # References include the `.import lib_export` in main.s and the `jsr lib_export` call.
    uris = {loc.uri for loc in result}
    assert any(u.endswith("src/main.s") for u in uris), f"got {uris}"


# ------------------------------------------------------------- qualified-name helper


def test_qualified_name_no_scope():
    from ca65_ls.types import Position as P
    from ca65_ls.types import Range as R
    from ca65_ls.types import SymbolKind, WorkspaceSymbol

    ws = WorkspaceSymbol(
        name="foo",
        kind=SymbolKind.LABEL,
        uri="file:///x",
        range=R(P(0, 0), P(0, 3)),
        selection_range=R(P(0, 0), P(0, 3)),
        scope_path=(),
        parent_label=None,
    )
    assert _qualified_name(ws) == "foo"


def test_qualified_name_with_scope():
    from ca65_ls.types import Position as P
    from ca65_ls.types import Range as R
    from ca65_ls.types import SymbolKind, WorkspaceSymbol

    ws = WorkspaceSymbol(
        name="foo",
        kind=SymbolKind.LABEL,
        uri="file:///x",
        range=R(P(0, 0), P(0, 3)),
        selection_range=R(P(0, 0), P(0, 3)),
        scope_path=("helpers",),
        parent_label=None,
    )
    assert _qualified_name(ws) == "helpers::foo"
