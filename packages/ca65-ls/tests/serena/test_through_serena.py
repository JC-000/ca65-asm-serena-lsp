"""What Serena's symbolic tools actually see. GREEN = parity with the direct
handlers and documented lifecycle; RED = defects from the 2026-09-02 review
(`xfail(strict=True, raises=AssertionError)`)."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

from ca65_ls.buffer.document import Document

from .conftest import create, started, write_project

red = pytest.mark.xfail(strict=True, raises=AssertionError)

_LSP_KIND = {
    12: "Function",
    5: "Class",
    14: "Constant",
    13: "Variable",
    11: "Interface",
    8: "Field",
    6: "Method",
    2: "Module",
    3: "Namespace",
    23: "Struct",
    10: "Enum",
}


def _flat(nodes, out=None):
    out = [] if out is None else out
    for n in nodes or []:
        out.append((n["name"], n["kind"], n["selectionRange"]["start"]["line"]))
        _flat(n.get("children"), out)
    return out


# ---------------------------------------------------------------- GREEN


def test_server_boots_and_reports_root(fixture_copy, data_dir):
    with started(fixture_copy, data_dir) as ls:
        assert ls.is_running()
        assert Path(ls.language_server.repository_root_path).resolve() == fixture_copy.resolve()


def test_document_symbols_match_direct_parse(fixture_copy, data_dir):
    with started(fixture_copy, data_dir) as ls:
        for rel in ("src/main.s", "src/helpers.s", "src/lib.s"):
            _all, roots = ls.request_document_symbols(rel).get_all_symbols_and_roots()
            via_serena = sorted((n, line) for n, _k, line in _flat(roots))
            doc = Document((fixture_copy / rel).as_uri(), (fixture_copy / rel).read_text())
            direct = sorted((s.name, s.selection_range.start.line) for s in doc.flat_symbols())
            assert via_serena == direct, rel


def test_definition_and_references_cross_files(fixture_copy, data_dir):
    with started(fixture_copy, data_dir) as ls:
        main = (fixture_copy / "src/main.s").read_text().splitlines()
        line = next(i for i, ln in enumerate(main) if "jsr" in ln and "lib_export" in ln)
        col = main[line].index("lib_export") + 1
        defs = ls.request_definition("src/main.s", line, col)
        assert [Path(d["relativePath"]).as_posix() for d in defs] == ["src/lib.s"]
        lib = (fixture_copy / "src/lib.s").read_text().splitlines()
        dline = next(i for i, ln in enumerate(lib) if ".proc lib_export" in ln)
        refs = ls.request_references("src/lib.s", dline, lib[dline].index("lib_export") + 1)
        assert any(
            Path(r["relativePath"]).as_posix() == "src/main.s"
            and r["range"]["start"]["line"] == line
            for r in refs
        )


def test_server_survives_repeated_start_stop(fixture_copy, data_dir):
    for i in range(3):
        with started(fixture_copy, data_dir / str(i)) as ls:
            assert ls.request_document_symbols("src/main.s").get_all_symbols_and_roots()[1]


def test_no_gitignored_file_is_offered(fixture_copy, data_dir):
    write_project(
        fixture_copy,
        {
            ".claude/worktrees/agent-1/src/main.s": ".proc ghost\n rts\n.endproc\n",
            ".gitignore": ".claude/\nbuild/*.o\nbuild/*.prg\n",
        },
    )
    with started(fixture_copy, data_dir, ignored=[".claude/"]) as ls:
        tree = ls.request_full_symbol_tree()
        names = {n["name"] for n in _walk(tree)}
        assert "ghost" not in names


def _walk(nodes):
    for n in nodes or []:
        yield n
        yield from _walk(n.get("children"))


# ---------------------------------------------------------------- RED


# (none open as of 2026-09-01; the 2026-09-02 review's F04/F09/F14 all flipped)


# ---------------------------------------------------------------- GREEN (fixes of 2026-09-01)


def test_new_file_on_disk_is_indexed_without_restart(fixture_copy, data_dir):
    """F04 (fixed 2026-09-01 in ca65-ls server.py): the index used to refresh
    only on didSave, which Serena never sends. It now reindexes on didChange
    and registers for workspace/didChangeWatchedFiles."""
    with started(fixture_copy, data_dir) as ls:
        write_project(
            fixture_copy,
            {"src/new_routine.s": ".export brand_new\n.proc brand_new\n rts\n.endproc\n"},
        )
        main = fixture_copy / "src/main.s"
        text = main.read_text() + "\n        jsr brand_new\n"
        main.write_text(text)
        line = len(text.splitlines()) - 1
        # Serena re-opens a document with its current text before asking.
        with ls.open_file("src/main.s"):
            defs = ls.request_definition("src/main.s", line, 14)
        assert [Path(d["relativePath"]).as_posix() for d in defs] == ["src/new_routine.s"]


def test_first_definition_request_is_fast(fixture_copy, data_dir):
    """F09 (fixed 2026-09-01): the shim overrides SolidLSP's fixed 2 s wait
    before the first cross-file request, since ca65-ls indexes synchronously
    in initialize."""
    with started(fixture_copy, data_dir) as ls:
        main = (fixture_copy / "src/main.s").read_text().splitlines()
        line = next(i for i, ln in enumerate(main) if "jsr" in ln and "lib_export" in ln)
        t0 = time.perf_counter()
        ls.request_definition("src/main.s", line, main[line].index("lib_export") + 1)
        assert time.perf_counter() - t0 < 0.5


def test_symlinked_project_root_yields_relative_paths(tmp_path, data_dir):
    """F14 (fixed 2026-09-01 in the shim): a project opened through a symlink
    used to report `../../..` relative paths and request_full_symbol_tree
    raised ValueError, because ca65-ls returns resolved URIs while SolidLSP
    compared them against the unresolved root. The shim now resolves the root."""
    real = write_project(tmp_path / "real", {"src/a.s": ".proc foo\n rts\n.endproc\n"})
    link = tmp_path / "link"
    link.symlink_to(real)
    with started(link, data_dir) as ls:
        try:
            tree = ls.request_full_symbol_tree()
        except ValueError as exc:  # the documented failure mode
            raise AssertionError(f"request_full_symbol_tree raised: {exc}") from None
        paths = {
            n.get("location", {}).get("relativePath") or n.get("relativePath") for n in _walk(tree)
        }
        assert not any(p and ".." in Path(p).parts for p in paths), paths


def test_symlinked_root_definition_paths_are_relative(tmp_path, data_dir):
    """F14 companion: definitions through a symlinked root come back as clean
    project-relative paths, not `../../..` walks to the real location."""
    real = write_project(
        tmp_path / "real",
        {
            "src/a.s": ".import foo\n.proc bar\n jsr foo\n rts\n.endproc\n",
            "src/b.s": ".export foo\n.proc foo\n rts\n.endproc\n",
        },
    )
    link = tmp_path / "link"
    link.symlink_to(real)
    with started(link, data_dir) as ls:
        defs = ls.request_definition("src/a.s", 2, 6)
        assert [Path(d["relativePath"]).as_posix() for d in defs] == ["src/b.s"]


def test_worktree_copies_are_invisible_without_gitignore(fixture_copy, data_dir):
    """F07: `.claude/worktrees/agent-*/` holds full copies of the project.
    Serena's own walk (full symbol tree, find_file, search_for_pattern) must
    skip `.claude` even when the project's .gitignore does not mention it and
    no ignored path is configured."""
    write_project(
        fixture_copy,
        {".claude/worktrees/agent-1/src/x.s": ".proc ghost\n rts\n.endproc\n"},
    )
    gitignore = fixture_copy / ".gitignore"
    if gitignore.exists():
        assert ".claude" not in gitignore.read_text()
    with started(fixture_copy, data_dir) as ls:
        names = {n["name"] for n in _walk(ls.request_full_symbol_tree())}
        assert "ghost" not in names
        assert "main" in names  # the walk still sees the real sources
        assert ls.is_ignored_path(".claude/worktrees/agent-1/src/x.s")
        for dirname in (".claude", ".serena", ".ca65-ls", "build", "obj"):
            assert ls.is_ignored_dirname(dirname), dirname


def test_missing_launcher_fails_fast_with_install_hint(fixture_copy, data_dir):
    """F12: a launcher that does not exist is reported before anything is
    spawned, naming the install command, instead of a generic initialize
    failure whose cause is on a separate log line."""
    ls = create(fixture_copy, data_dir, settings={"ls_base_cmd": ["/nonexistent/ca65-python"]})
    t0 = time.perf_counter()
    try:
        with pytest.raises(RuntimeError) as exc:
            ls.start()
    finally:
        ls.stop()
    assert time.perf_counter() - t0 < 2.0
    message = str(exc.value)
    assert "/nonexistent/ca65-python" in message
    assert "not found" in message
    assert "pip install ca65-ls" in message
    assert "uv pip install -e ~/Documents/ca65-asm-serena-lsp/packages/ca65-ls" in message


def test_hanging_server_fails_within_initialize_timeout(fixture_copy, data_dir):
    """F12: a ca65-ls that never answers `initialize` used to take the full
    request timeout (235 s in the review). The shim bounds initialize
    separately (`initialize_timeout`, default 60 s)."""
    ls = create(
        fixture_copy,
        data_dir,
        settings={
            "ls_base_cmd": [sys.executable],
            "ls_args": ["-c", "import time; time.sleep(60)"],
            "initialize_timeout": 2,
        },
    )
    t0 = time.perf_counter()
    try:
        with pytest.raises(RuntimeError) as exc:
            ls.start()
    finally:
        ls.stop()
    assert time.perf_counter() - t0 < 10.0
    assert "did not answer `initialize` within 2 s" in str(exc.value)
    assert "initialize_timeout" in str(exc.value)


def test_crashing_server_reports_install_hint(fixture_copy, data_dir):
    """F12: a ca65-ls that exits during startup (e.g. a broken install) is
    reported as such, with the install command, within seconds."""
    ls = create(
        fixture_copy,
        data_dir,
        settings={
            "ls_base_cmd": [sys.executable],
            "ls_args": [
                "-c",
                "import sys; sys.stderr.write('boom: simulated crash\\n'); sys.exit(3)",
            ],
        },
    )
    t0 = time.perf_counter()
    try:
        with pytest.raises(RuntimeError) as exc:
            ls.start()
    finally:
        ls.stop()
    assert time.perf_counter() - t0 < 10.0
    message = str(exc.value)
    assert "exited during `initialize`" in message or "terminated immediately" in message
    assert "pip install ca65-ls" in message or "terminated immediately" in message


def test_generic_request_timeout_is_restored_after_initialize(fixture_copy, data_dir):
    """The initialize bound must not leak into ordinary requests."""
    with started(fixture_copy, data_dir, timeout=77.0, settings={"initialize_timeout": 5}) as ls:
        assert ls.server._request_timeout == 77.0  # noqa: SLF001 (no public getter)


def test_pygls_protocol_chatter_is_not_logged_as_error():
    """F10 (shim side): stderr lines that pygls emits for its own protocol
    traffic are chatter even when the payload mentions `error`; the default
    classifier flagged them as ERROR. Real errors keep their level."""
    from ca65_ls.serena_adapter import Ca65LanguageServer

    classify = Ca65LanguageServer._determine_log_level
    chatter = 'INFO:pygls.protocol.json_rpc:Sending data: {"name": "ip65_error", "kind": 12}'
    assert classify(chatter) == logging.DEBUG
    assert classify("DEBUG:pygls.server:Received data") == logging.DEBUG
    assert classify("INFO:ca65_ls.index.workspace:indexed 42 files") == logging.INFO
    assert classify("ERROR:ca65_ls.server:reindex failed") == logging.ERROR
    assert (
        classify(
            "Traceback (most recent call last): ... ModuleNotFoundError: No module named 'ca65_ls'"
        )
        == logging.ERROR
    )
