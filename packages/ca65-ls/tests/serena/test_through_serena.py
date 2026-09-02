"""What Serena's symbolic tools actually see. GREEN = parity with the direct
handlers and documented lifecycle; RED = defects from the 2026-09-02 review
(`xfail(strict=True, raises=AssertionError)`)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ca65_ls.buffer.document import Document

from .conftest import started, write_project

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


@red
def test_first_definition_request_is_fast(fixture_copy, data_dir):
    """F09: the shim inherits SolidLSP's fixed 2 s wait before the first
    cross-file request although ca65-ls indexes synchronously in initialize.
    Every Serena session pays it once per server start."""
    with started(fixture_copy, data_dir) as ls:
        main = (fixture_copy / "src/main.s").read_text().splitlines()
        line = next(i for i, ln in enumerate(main) if "jsr" in ln and "lib_export" in ln)
        t0 = time.perf_counter()
        ls.request_definition("src/main.s", line, main[line].index("lib_export") + 1)
        assert time.perf_counter() - t0 < 0.5


@red
def test_new_file_on_disk_is_indexed_without_restart(fixture_copy, data_dir):
    """F04: the workspace index only refreshes on didSave, which Serena never
    sends; didChange and didChangeWatchedFiles do nothing. After an agent
    creates or edits a routine, definition/references stay stale until the
    language server is restarted."""
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


@red
def test_symlinked_project_root_yields_relative_paths(tmp_path, data_dir):
    """F14: a project opened through a symlink reports `../../..` relative
    paths and request_full_symbol_tree raises ValueError."""
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
