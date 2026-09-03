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


# ------------------------------------------------- review fixes 2026-09-02
#
# Green regression guards for the server defects found by the 2026-09-02
# review: index refresh on didOpen / didChange / didClose / watched files,
# scope-aware definition ranking, identifier resolution, and quiet logging.


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _fresh_server(root: Path) -> Ca65LanguageServer:
    from ca65_ls.index.workspace import WorkspaceIndex
    from ca65_ls.server import _path_to_uri

    server = Ca65LanguageServer()
    idx = WorkspaceIndex(root, cache=False)
    idx.reindex()
    server.indexes[_path_to_uri(root)] = idx
    server.workspace_roots.append(root)
    return server


def _did_change(server: Ca65LanguageServer, uri: str, *changes) -> None:
    srv.on_did_change(
        server,
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(uri=uri, version=2),
            content_changes=list(changes),
        ),
    )


def _definition(server: Ca65LanguageServer, path: Path, line_substr: str, ident: str):
    return (
        srv.on_definition(
            server,
            lsp.DefinitionParams(
                text_document=lsp.TextDocumentIdentifier(uri=_uri(path)),
                position=_position_of(path, line_substr, ident),
            ),
        )
        or []
    )


def _names(locs) -> list[str]:
    return sorted(srv._uri_to_path(loc.uri).name for loc in locs)


def _index(server: Ca65LanguageServer):
    return next(iter(server.indexes.values()))


# ---- didChange / didOpen / didClose keep the index current


def test_did_change_full_text_reindexes_from_the_buffer(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open_file(server, main)
    new_text = main.read_text() + "\n.proc added_by_edit\n        rts\n.endproc\n"
    _did_change(server, uri, lsp.TextDocumentContentChangeWholeDocument(text=new_text))
    assert server.documents[uri].text == new_text
    [sym] = _index(server).lookup("added_by_edit")
    assert sym.uri == uri
    # The edit is visible to definition without any save.
    srv.on_did_close(server, lsp.DidCloseTextDocumentParams(lsp.TextDocumentIdentifier(uri=uri)))
    assert _index(server).lookup("added_by_edit") == [], "didClose restores the on-disk view"


def test_did_change_incremental_edit_is_applied_to_the_buffer(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open_file(server, main)
    lines = main.read_text().splitlines()
    end = len(lines)
    insert = lsp.TextDocumentContentChangePartial(
        range=lsp.Range(start=lsp.Position(end, 0), end=lsp.Position(end, 0)),
        text=".proc inserted_incrementally\n        rts\n.endproc\n",
    )
    _did_change(server, uri, insert)
    assert "inserted_incrementally" in server.documents[uri].text
    assert lines[0] in server.documents[uri].text, "the rest of the file survived"
    assert len(_index(server).lookup("inserted_incrementally")) == 1


def test_apply_content_change_replaces_a_range():
    text = "        jsr     old_name\n        rts\n"
    change = lsp.TextDocumentContentChangePartial(
        range=lsp.Range(start=lsp.Position(0, 16), end=lsp.Position(0, 24)), text="new_name"
    )
    assert srv._apply_content_change(text, change) == "        jsr     new_name\n        rts\n"
    whole = lsp.TextDocumentContentChangeWholeDocument(text="x: rts\n")
    assert srv._apply_content_change(text, whole) == "x: rts\n"


def test_did_open_picks_up_files_created_on_disk_since_the_last_request(ls):
    """Serena's path: files appear on disk (an agent wrote them), then the
    caller is opened with its new text; no didSave, no watcher events."""
    server, root = ls
    _write(root, {"src/new_routine.s": ".export brand_new\n.proc brand_new\n rts\n.endproc\n"})
    main = root / "src" / "main.s"
    main.write_text(main.read_text() + "\n        jsr     brand_new\n")
    _open_file(server, main)
    assert _names(_definition(server, main, "jsr     brand_new", "brand_new")) == ["new_routine.s"]


def test_query_refresh_is_throttled_but_eventually_sees_disk_changes(ls, monkeypatch):
    server, root = ls
    main = root / "src" / "main.s"
    _open_file(server, main)
    _write(root, {"src/late.s": ".proc late_arrival\n rts\n.endproc\n"})
    idx = _index(server)
    idx._last_refresh = __import__("time").monotonic()
    params = lsp.WorkspaceSymbolParams(query="late_arrival")
    assert srv.on_workspace_symbol(server, params) == [], "within REFRESH_MAX_AGE: not rescanned"
    monkeypatch.setattr(srv, "REFRESH_MAX_AGE", 0.0)
    assert [s.name for s in srv.on_workspace_symbol(server, params)] == ["late_arrival"]


# ---- workspace/didChangeWatchedFiles


def _watched(server: Ca65LanguageServer, *events: tuple[Path, lsp.FileChangeType]) -> None:
    srv.on_did_change_watched_files(
        server,
        lsp.DidChangeWatchedFilesParams(
            changes=[lsp.FileEvent(uri=_uri(p), type=t) for p, t in events]
        ),
    )


def test_watched_files_create_change_delete(ls):
    server, root = ls
    idx = _index(server)
    new = root / "src" / "watched.s"
    files_before = idx.stats["files"]

    _write(root, {"src/watched.s": ".proc watched_proc\n jsr lib_export\n.endproc\n"})
    _watched(server, (new, lsp.FileChangeType.Created))
    assert idx.stats["files"] == files_before + 1
    assert [s.uri for s in idx.lookup("watched_proc")] == [_uri(new)]
    assert any(r.uri == _uri(new) for r in idx.references("lib_export"))

    new.write_text(".proc watched_proc2\n rts\n.endproc\n")
    _watched(server, (new, lsp.FileChangeType.Changed))
    assert idx.lookup("watched_proc") == []
    assert len(idx.lookup("watched_proc2")) == 1
    assert not any(r.uri == _uri(new) for r in idx.references("lib_export"))

    new.unlink()
    _watched(server, (new, lsp.FileChangeType.Deleted))
    assert idx.lookup("watched_proc2") == []
    assert idx.stats["files"] == files_before


def test_watched_files_ignore_rules_still_apply(ls):
    server, root = ls
    ghost = root / ".claude" / "worktrees" / "agent-1" / "src" / "ghost.s"
    _write(root, {".claude/worktrees/agent-1/src/ghost.s": ".proc ghost\n rts\n.endproc\n"})
    _watched(server, (ghost, lsp.FileChangeType.Created))
    assert _index(server).lookup("ghost") == []


def test_watched_files_dbg_change_reloads_addresses(ls):
    server, root = ls
    idx = _index(server)
    dbg = root / "build" / "test_repo.dbg"
    [before] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert before.address == 0x822
    dbg.unlink()
    _watched(server, (dbg, lsp.FileChangeType.Deleted))
    [after] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert after.address is None


def test_watched_files_registration_targets_sources_and_dbg():
    params = srv._watched_files_registration()
    [reg] = params.registrations
    assert reg.method == lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES
    globs = [w.glob_pattern for w in reg.register_options.watchers]
    assert any(".dbg" in g for g in globs) and any("inc" in g for g in globs)
    assert srv._client_watches_files(None) is False
    caps = lsp.ClientCapabilities(
        workspace=lsp.WorkspaceClientCapabilities(
            did_change_watched_files=lsp.DidChangeWatchedFilesClientCapabilities(
                dynamic_registration=True
            )
        )
    )
    assert srv._client_watches_files(caps) is True


# ---- definition ranking


PROC_WITH_DONE = (
    ".proc {name}\n        ldx #0\ndone:\n        dex\n        bne done\n        rts\n.endproc\n"
)


def test_definition_prefers_the_enclosing_routines_own_label(tmp_path):
    _write(
        tmp_path, {"a.s": PROC_WITH_DONE.format(name="a"), "b.s": PROC_WITH_DONE.format(name="b")}
    )
    server = _fresh_server(tmp_path)
    locs = _definition(server, tmp_path / "b.s", "bne done", "done")
    assert [(srv._uri_to_path(loc.uri).name, loc.range.start.line) for loc in locs] == [("b.s", 2)]


def test_definition_prefers_the_calling_files_own_label_over_a_foreign_proc(tmp_path):
    _write(
        tmp_path,
        {
            "boot.s": "print_string:\n        rts\n        jsr print_string\n",
            "other/main.s": ".proc print_string\n        rts\n.endproc\n",
        },
    )
    server = _fresh_server(tmp_path)
    assert _names(_definition(server, tmp_path / "boot.s", "jsr print_string", "print_string")) == [
        "boot.s"
    ]


def test_definition_of_an_imported_name_prefers_the_file_that_exports_it(tmp_path):
    _write(
        tmp_path,
        {
            "main.s": ".import foo\n        jsr foo\n",
            "lib.s": ".export foo\n.proc foo\n        rts\n.endproc\n",
            "private.s": ".proc foo\n        rts\n.endproc\n",
        },
    )
    server = _fresh_server(tmp_path)
    assert _names(_definition(server, tmp_path / "main.s", "jsr foo", "foo")) == ["lib.s"]


def test_definition_falls_back_to_kind_preference_without_scope_hints(tmp_path):
    _write(
        tmp_path,
        {
            "main.s": "        jsr foo\n",
            "impl.s": ".proc foo\n        rts\n.endproc\n",
            "decl.s": ".import foo\n",
        },
    )
    server = _fresh_server(tmp_path)
    assert _names(_definition(server, tmp_path / "main.s", "jsr foo", "foo")) == ["impl.s"]


def test_hover_follows_the_same_ranking_as_definition(tmp_path):
    _write(
        tmp_path, {"a.s": PROC_WITH_DONE.format(name="a"), "b.s": PROC_WITH_DONE.format(name="b")}
    )
    server = _fresh_server(tmp_path)
    b = tmp_path / "b.s"
    hover = srv.on_hover(
        server,
        lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri=_uri(b)),
            position=_position_of(b, "bne done", "done"),
        ),
    )
    assert hover is not None and hover.range.start.line == 2


# ---- identifier resolution


def test_identifier_at_falls_through_from_mnemonic_and_directive_to_operand():
    assert _identifier_at("        jsr     foo\n", lsp.Position(0, 9)) == "foo"
    assert _identifier_at("        BNE     @loop\n", lsp.Position(0, 10)) == "@loop"
    assert _identifier_at(".proc   bar\n", lsp.Position(0, 2)) == "bar"
    assert _identifier_at(".export baz, qux\n", lsp.Position(0, 3)) == "baz"
    # A mnemonic with no operand identifier is still returned as itself.
    assert _identifier_at("        rts\n", lsp.Position(0, 9)) == "rts"
    # A directive followed by another directive does not skip.
    assert _identifier_at(".if .defined(x)\n", lsp.Position(0, 1)) == "if"


def test_prepare_rename_from_mnemonic_offers_the_operand(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open_file(server, main)
    line = next(
        i for i, ln in enumerate(main.read_text().splitlines()) if "jsr     lib_export" in ln
    )
    result = srv.on_prepare_rename(
        server,
        lsp.PrepareRenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri), position=lsp.Position(line, 9)
        ),
    )
    assert result is not None and result.placeholder == "lib_export"
    assert result.range.start.character == main.read_text().splitlines()[line].index("lib_export")


# ---- workspace/symbol dedupe


def test_workspace_symbol_keeps_distinct_cheap_locals_with_one_name(tmp_path):
    _write(
        tmp_path,
        {
            "a.s": "first:\n@loop:\n        bne @loop\n        rts\nsecond:\n@loop:\n        bne @loop\n        rts\n"
        },
    )
    server = _fresh_server(tmp_path)
    hits = srv.on_workspace_symbol(server, lsp.WorkspaceSymbolParams(query="@loop"))
    assert sorted(h.location.range.start.line for h in hits) == [1, 5]


# ---- logging


def test_configure_logging_silences_pygls_protocol_chatter():
    import logging

    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    try:
        srv.configure_logging("INFO")
        for name in ("pygls", "pygls.protocol.json_rpc", "lsprotocol"):
            assert not logging.getLogger(name).isEnabledFor(logging.INFO), name
            assert logging.getLogger(name).isEnabledFor(logging.WARNING), name
        assert logging.getLogger("ca65-ls").isEnabledFor(logging.INFO)

        srv.configure_logging("INFO", verbose=True)
        assert logging.getLogger("pygls.protocol.json_rpc").isEnabledFor(logging.DEBUG)
    finally:
        for name in srv.PROTOCOL_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]


def test_main_verbose_flag_defaults_from_env(monkeypatch):
    calls = []
    monkeypatch.setattr(
        srv, "configure_logging", lambda level, verbose: calls.append((level, verbose))
    )
    monkeypatch.setattr(srv.server, "start_io", lambda: None)
    monkeypatch.delenv(srv.VERBOSE_ENV, raising=False)
    srv.main(["--stdio"])
    monkeypatch.setenv(srv.VERBOSE_ENV, "1")
    srv.main(["--stdio", "--log-level", "warning"])
    assert calls == [("INFO", False), ("warning", True)]


# ---- scope from the definition site


def test_references_and_rename_from_the_label_definition_stay_in_its_proc(tmp_path):
    """With the cursor on `done:` itself, the label's own body is the smallest
    container; the proc around it must be used instead, or references and
    rename go project-wide."""
    _write(
        tmp_path, {"a.s": PROC_WITH_DONE.format(name="a"), "b.s": PROC_WITH_DONE.format(name="b")}
    )
    server = _fresh_server(tmp_path)
    a = tmp_path / "a.s"
    refs = srv.on_references(
        server,
        lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri=_uri(a)),
            position=_position_of(a, "done:", "done"),
            context=lsp.ReferenceContext(include_declaration=False),
        ),
    )
    assert [(srv._uri_to_path(r.uri).name, r.range.start.line) for r in refs] == [("a.s", 4)]
    edit = srv.on_rename(
        server,
        lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=_uri(a)),
            position=_position_of(a, "done:", "done"),
            new_name="finish",
        ),
    )
    assert {
        srv._uri_to_path(u).name: sorted(e.range.start.line for e in es)
        for u, es in edit.changes.items()
    } == {"a.s": [2, 4]}


def test_did_open_forces_a_rescan_only_when_the_opened_file_changed_on_disk(ls):
    server, root = ls
    idx = _index(server)
    main = root / "src" / "main.s"
    helpers = root / "src" / "helpers.s"
    _write(root, {"src/late.s": ".proc late_arrival\n rts\n.endproc\n"})

    # Unchanged file just after a reindex: throttled, so the new file is not seen yet.
    _open_file(server, helpers)
    assert idx.lookup("late_arrival") == []

    # A file that changed on disk since it was indexed forces the scan.
    main.write_text(main.read_text() + "\n        jsr     late_arrival\n")
    _open_file(server, main)
    assert [srv._uri_to_path(s.uri).name for s in idx.lookup("late_arrival")] == ["late.s"]
    assert _names(_definition(server, main, "jsr     late_arrival", "late_arrival")) == ["late.s"]
