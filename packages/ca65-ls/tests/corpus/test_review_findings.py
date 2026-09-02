"""RED tests from the adversarial review of 2026-09-02, corpus scale.

Synthetic reproducers for the same findings live in tests/test_review_red.py.
These versions measure the defects on the real projects, so a partial fix
that handles the toy case but not the corpus still fails the gate.
"""

from __future__ import annotations

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.types import SymbolKind, WorkspaceSymbol

from .conftest import Project

red = pytest.mark.xfail(strict=True, raises=AssertionError)


def _rename(project: Project, sym: WorkspaceSymbol, new_name: str) -> dict[str, list[int]]:
    params = lsp.RenameParams(
        text_document=lsp.TextDocumentIdentifier(uri=sym.uri),
        position=lsp.Position(
            line=sym.selection_range.start.line, character=sym.selection_range.start.character + 1
        ),
        new_name=new_name,
    )
    edit = srv.on_rename(project.server, params)
    changes = (edit.changes if edit else None) or {}
    return {u: sorted(e.range.start.line for e in edits) for u, edits in changes.items()}


def _proc_ranges(project: Project) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for s in project.symbols:
        if s.kind == SymbolKind.PROC:
            out.setdefault(s.uri, []).append((s.range.start.line, s.range.end.line))
    return out


def _proc_local_labels(project: Project) -> list[tuple[WorkspaceSymbol, tuple[int, int]]]:
    """LABEL symbols lexically inside a .proc, with that proc's line range."""
    ranges = _proc_ranges(project)
    out = []
    for s in project.symbols:
        if s.kind != SymbolKind.LABEL:
            continue
        line = s.selection_range.start.line
        for lo, hi in ranges.get(s.uri, ()):
            if lo < line < hi:
                out.append((s, (lo, hi)))
                break
    return out


@red
def test_rename_of_proc_local_label_never_leaves_its_proc(corpus: list[Project]):
    """F3 (destructive): c64-mlkem sponge.s `done:` rename edits four other
    routines in four files. 36 of 107 proc-nested labels overflow."""
    leaks = []
    for project in corpus:
        seen: set[str] = set()
        for sym, (lo, hi) in _proc_local_labels(project):
            if sym.name in seen:
                continue
            seen.add(sym.name)
            for uri, lines in _rename(project, sym, sym.name + "_x").items():
                if uri != sym.uri or any(not (lo <= ln <= hi) for ln in lines):
                    leaks.append(
                        (
                            project.root.name,
                            project.path(sym.uri).name,
                            sym.name,
                            project.path(uri).name,
                            lines[:5],
                        )
                    )
            if len(seen) >= 25:
                break
    assert not leaks, f"{len(leaks)} renames leaked outside their proc; first: {leaks[:8]}"


@red
def test_rename_of_exported_proc_edits_the_declarator(corpus: list[Project]):
    """F4: the declarator was dropped from references by the export
    suppression fix, so rename leaves `.export old` behind (3885 sites)."""
    misses = []
    for project in corpus:
        checked = 0
        for sym in project.symbols:
            if sym.kind != SymbolKind.PROC or sym.scope_path:
                continue
            lines = project.lines(project.path(sym.uri))
            decl = [
                i
                for i, ln in enumerate(lines)
                if ln.strip().startswith((".export", ".exportzp", ".global"))
                and sym.name in ln.replace(",", " ").split()
            ]
            if not decl:
                continue
            edits = _rename(project, sym, sym.name + "_x").get(sym.uri, [])
            if not set(decl) <= set(edits):
                misses.append(
                    (project.root.name, project.path(sym.uri).name, sym.name, decl, edits[:4])
                )
            checked += 1
            if checked >= 15:
                break
    assert not misses, f"{len(misses)} renames skipped the .export line; first: {misses[:8]}"


@red
def test_labels_without_colons_files_are_indexed(corpus: list[Project]):
    """F6: `.feature labels_without_colons` turns the whole file into one
    ERROR node; c64-https loses 656 labels in its three vt100 drivers."""
    thin = []
    for project in corpus:
        for path in project.files:
            lines = project.lines(path)
            if not any("labels_without_colons" in ln for ln in lines[:60]):
                continue
            col0 = {
                ln.split()[0]
                for ln in lines
                if ln
                and not ln[0].isspace()
                and not ln.startswith((";", ".", "@", "*"))
                and ln.split()[0].replace("_", "a").isalnum()
            }
            indexed = {s.name for s in project.by_uri.get(project.uri(path), [])}
            missing = col0 - indexed
            if col0 and len(missing) > 0.2 * len(col0):
                thin.append(
                    (
                        project.root.name,
                        path.relative_to(project.root).as_posix(),
                        len(col0),
                        len(indexed),
                        sorted(missing)[:5],
                    )
                )
    assert not thin, f"{len(thin)} labels_without_colons files mostly unindexed; first: {thin[:6]}"


@red
def test_definition_prefers_the_label_in_the_calling_file(corpus: list[Project]):
    """F10: when a name is a LABEL in the caller's own file and a PROC in an
    unrelated file, on_definition returns the foreign PROC (c64-wireguard
    print_string: 21 such names)."""
    wrong = []
    for project in corpus:
        by_name: dict[str, list[WorkspaceSymbol]] = {}
        for s in project.symbols:
            if s.kind in (SymbolKind.PROC, SymbolKind.LABEL) and not s.scope_path:
                by_name.setdefault(s.name, []).append(s)
        for name, defs in by_name.items():
            kinds = {d.kind for d in defs}
            if kinds != {SymbolKind.PROC, SymbolKind.LABEL}:
                continue
            for label in (d for d in defs if d.kind == SymbolKind.LABEL):
                lines = project.lines(project.path(label.uri))
                call = next(
                    (
                        i
                        for i, ln in enumerate(lines)
                        if ln.split(";")[0].split()[:2] in (["jsr", name], ["jmp", name])
                    ),
                    None,
                )
                if call is None:
                    continue
                pos = lsp.Position(line=call, character=lines[call].index(name) + 1)
                locs = (
                    srv.on_definition(
                        project.server,
                        lsp.DefinitionParams(
                            text_document=lsp.TextDocumentIdentifier(uri=label.uri), position=pos
                        ),
                    )
                    or []
                )
                if not any(
                    loc.uri == label.uri
                    and loc.range.start.line == label.selection_range.start.line
                    for loc in locs
                ):
                    wrong.append(
                        (
                            project.root.name,
                            project.path(label.uri).name,
                            name,
                            [project.path(loc.uri).name for loc in locs][:3],
                        )
                    )
                break
    assert not wrong, f"{len(wrong)} definitions skipped the same-file label; first: {wrong[:8]}"
