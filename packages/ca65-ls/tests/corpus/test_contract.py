"""Invariants the symbolic tools depend on, checked against every corpus project.

GREEN tests are regression guards. RED tests (``xfail(strict=True)``) record
defects found by adversarial review that are not fixed yet; when a fix lands
they XPASS, the run fails, and the marker is removed. See the module docstring
of tests/hook/test_bash_nudge.py in the repo root for the convention.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.types import SymbolKind, WorkspaceSymbol

from . import ground_truth as gt
from .conftest import Project

red = pytest.mark.xfail(strict=True, raises=AssertionError)

#: Kinds whose selection_range must spell the symbol's own name in the source.
_NAMED_KINDS = {
    SymbolKind.PROC,
    SymbolKind.SCOPE,
    SymbolKind.MACRO,
    SymbolKind.STRUCT,
    SymbolKind.UNION,
    SymbolKind.ENUM,
    SymbolKind.LABEL,
    SymbolKind.CHEAP_LOCAL,
    SymbolKind.CONSTANT,
    SymbolKind.IMPORT,
    SymbolKind.EXPORT,
    SymbolKind.FIELD,
}
_DEFINITION_KINDS = {
    SymbolKind.PROC,
    SymbolKind.LABEL,
    SymbolKind.MACRO,
    SymbolKind.CONSTANT,
    SymbolKind.SCOPE,
}
_SAMPLE = 60


def _slice(project: Project, sym: WorkspaceSymbol) -> str:
    lines = project.lines(project.path(sym.uri))
    r = sym.selection_range
    if r.start.line != r.end.line:
        return "<multi-line>"
    return lines[r.start.line][r.start.character : r.end.character]


def _all(project: Project) -> list[WorkspaceSymbol]:
    return project.symbols


# ------------------------------------------------------------------ building


def test_index_builds_with_files_and_symbols(project: Project):
    stats = project.index.stats
    assert stats.get("files", 0) > 0, stats
    assert stats.get("symbols", 0) > 0, stats
    assert len(project.files) == stats["files"], "index and file walk disagree"


def test_cold_reindex_is_within_budget(project: Project):
    from ca65_ls.index.workspace import WorkspaceIndex

    start = time.perf_counter()
    WorkspaceIndex(project.root, cache=False).reindex()
    elapsed = time.perf_counter() - start
    budget = 2.0 + 0.01 * len(project.files)  # 2s + 10ms/file
    assert elapsed < budget, f"{elapsed:.2f}s for {len(project.files)} files (budget {budget:.1f}s)"


def test_reindex_is_deterministic(project: Project):
    from ca65_ls.index.workspace import WorkspaceIndex

    again = WorkspaceIndex(project.root, cache=False)
    again.reindex()
    assert Counter(_all(project)) == Counter(again.all_symbols())


# ------------------------------------------------------------------ positions


def test_selection_range_spells_the_symbol_name(project: Project):
    bad = []
    for sym in _all(project):
        if sym.kind not in _NAMED_KINDS:
            continue
        text = _slice(project, sym)
        if text != sym.name:
            bad.append(
                (
                    project.path(sym.uri).relative_to(project.root),
                    sym.selection_range.start.line + 1,
                    sym.kind.value,
                    sym.name,
                    text,
                )
            )
    assert not bad, f"{len(bad)} symbols whose selection_range is not their name; first: {bad[:10]}"


def test_ranges_are_well_formed(project: Project):
    bad = []
    for sym in _all(project):
        n = len(project.lines(project.path(sym.uri)))
        r, s = sym.range, sym.selection_range
        ok = (
            (r.start.line, r.start.character) <= (r.end.line, r.end.character)
            and (s.start.line, s.start.character) <= (s.end.line, s.end.character)
            and r.start.line <= s.start.line
            and s.end.line <= r.end.line
            and r.end.line <= n
        )
        if not ok:
            bad.append((project.path(sym.uri).name, sym.name, sym.kind.value, r, s))
    assert not bad, f"{len(bad)} malformed ranges; first: {bad[:5]}"


def _nesting_violations(project: Project) -> list:
    bad = []

    def walk(node: lsp.DocumentSymbol, path: str):
        for child in node.children or ():
            inside = (node.range.start.line, node.range.start.character) <= (
                child.range.start.line,
                child.range.start.character,
            ) and (
                child.range.end.line,
                child.range.end.character,
            ) <= (node.range.end.line, node.range.end.character)
            if not inside:
                bad.append((project.root.name, path, node.name, child.name))
            walk(child, path)

    for path in project.files:
        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri=project.uri(path))
        )
        for top in srv.on_document_symbol(project.server, params) or []:
            walk(top, str(path.relative_to(project.root)))
    return bad


@red
def test_document_symbol_children_nest_inside_parents(corpus: list[Project]):
    """RED (found 2026-09-02 by this suite): a label that is the last one
    before `.endproc` gets a body that runs to end-of-file instead of stopping
    at the enclosing proc's end (c64-x25519 fe25519.s: mul38_hi 1160-2481
    inside mul_by_38 1115-1161). Label bodies must be clipped to their parent.
    """
    bad = [v for project in corpus for v in _nesting_violations(project)]
    assert not bad, f"{len(bad)} children outside their parent's range; first: {bad[:10]}"


# ------------------------------------------------------------------ duplicates


@red
def test_no_symbol_is_emitted_twice(corpus: list[Project]):
    """RED (found 2026-09-02 by this suite): every cheap local, and labels
    nested in procs, reach the workspace index twice as byte-identical records
    (c64-x25519: 197 of 885 records). The document-symbol tree shows them once,
    so the duplication happens during ingestion, not parsing.
    """
    seen = Counter(
        (
            s.uri,
            s.name,
            s.kind,
            s.selection_range.start.line,
            s.selection_range.start.character,
            s.scope_path,
        )
        for project in corpus
        for s in _all(project)
    )
    dupes = [
        (k[0].rsplit("/", 1)[-1], k[1], k[2].value, k[3] + 1, n) for k, n in seen.items() if n > 1
    ]
    assert not dupes, f"{len(dupes)} duplicated symbols; first: {dupes[:10]}"


def test_export_declarator_suppressed_when_defined_in_same_file(project: Project):
    """Regression guard for the 24.7% phantom-duplicate bug fixed 2026-08-28."""
    defined: dict[str, set[str]] = defaultdict(set)
    for s in _all(project):
        if s.kind not in (SymbolKind.EXPORT, SymbolKind.IMPORT, SymbolKind.SEGMENT):
            defined[s.uri].add(s.name)
    leaks = [
        (project.path(s.uri).name, s.name)
        for s in _all(project)
        if s.kind == SymbolKind.EXPORT and s.name in defined[s.uri]
    ]
    assert not leaks, (
        f"{len(leaks)} .export declarators duplicating an in-file definition; first: {leaks[:10]}"
    )


@red
def test_duplicate_rate_stays_low(corpus: list[Project]):
    """RED: same root cause as test_no_symbol_is_emitted_twice (22.3% on
    c64-x25519). Kept separate because it also catches a *new* phantom source
    that emits under a different kind once the identical-record bug is fixed.

    Same (file, name, line) reported under two kinds. Some is legitimate
    (a label that is also .export'ed elsewhere in the file); a jump means a
    new phantom source."""
    by_site = Counter(
        (s.uri, s.name, s.selection_range.start.line) for project in corpus for s in _all(project)
    )
    total = sum(by_site.values())
    dup = sum(n - 1 for n in by_site.values() if n > 1)
    rate = dup / total if total else 0.0
    assert rate < 0.05, f"{rate:.1%} duplicate rate ({dup}/{total})"


# ------------------------------------------------------------------ file walk

_TOOL_DIRS = (".claude/", ".git/", ".ca65-ls/", ".serena/", ".venv/", "node_modules/")


def _contaminated(project: Project) -> bool:
    return any(
        f"/{d}" in f"/{project.path(s.uri).relative_to(project.root).as_posix()}"
        for s in _all(project)
        for d in _TOOL_DIRS
    )


@red
def test_tool_directories_are_not_indexed(corpus: list[Project]):
    """RED (found 2026-09-02 by this suite): `.claude/worktrees/agent-*/` holds
    complete copies of the repo left behind by worktree-isolated agents. They
    are gitignored (`.claude/*`), but the indexer still walks them, so on
    c64-https 1748 of 1995 indexed files are copies and every routine has
    7-34 definitions. Five of the nine corpus projects are affected.
    """
    offenders = sorted(
        {
            (
                project.root.name,
                project.path(s.uri).relative_to(project.root).as_posix().split("/")[0],
            )
            for project in corpus
            for s in _all(project)
            if _contaminated_uri(project, s.uri)
        }
    )
    assert not offenders, f"symbols indexed under tool directories: {offenders}"


def _contaminated_uri(project: Project, uri: str) -> bool:
    rel = "/" + project.path(uri).relative_to(project.root).as_posix()
    return any(f"/{d}" in rel for d in _TOOL_DIRS)


@red
def test_acme_dialect_files_contribute_no_symbols(corpus: list[Project]):
    """RED (found 2026-09-02 by this suite): c64-nist-curves keeps ACME
    (`!zone`) and CA65 twins side by side (mod256.asm / mod256.s). The CA65
    grammar parses the ACME file into partial garbage, so `jsr fp_mod_mul` in
    the .asm twin shows up in some queries and not others. Files that carry
    ACME directives should be sniffed and skipped by the indexer.
    """
    offenders = [
        (
            project.root.name,
            path.relative_to(project.root).as_posix(),
            len(project.by_uri.get(project.uri(path), [])),
        )
        for project in corpus
        for path in project.files
        if gt.is_acme(path) and project.by_uri.get(project.uri(path))
    ]
    assert not offenders, f"{len(offenders)} ACME files indexed as CA65; first: {offenders[:10]}"


def test_gitignored_files_are_not_indexed(project: Project):
    """Whatever the project's own .gitignore excludes must stay out of the index."""
    import subprocess

    if not (project.root / ".git").exists():
        pytest.skip("not a git checkout")
    rels = [project.path(s.uri).relative_to(project.root).as_posix() for s in _all(project)]
    rels = sorted(set(rels))
    out = subprocess.run(
        ["git", "-C", str(project.root), "check-ignore", "--stdin"],
        input="\n".join(rels),
        capture_output=True,
        text=True,
    )
    ignored = [line for line in out.stdout.splitlines() if line]
    if _contaminated(project):
        pytest.xfail(
            f"{len(ignored)} gitignored files indexed; see test_tool_directories_are_not_indexed"
        )
    assert not ignored, f"{len(ignored)} gitignored files indexed; first: {ignored[:10]}"


# ------------------------------------------------------------------ coverage


def test_every_block_declaration_is_indexed(project: Project):
    missing = []
    for path in project.files:
        if gt.is_acme(path):
            continue
        in_file = project.by_uri.get(project.uri(path), [])
        indexed = {(s.name, s.kind.value, s.selection_range.start.line) for s in in_file}
        names_at_line = {(s.name, s.selection_range.start.line) for s in in_file}
        for decl in gt.block_declarations(path):
            # Tolerate a different kind at the same site (e.g. .mac aliases), not absence.
            if (decl.name, decl.kind, decl.line) not in indexed and (
                decl.name,
                decl.line,
            ) not in names_at_line:
                missing.append(
                    (str(path.relative_to(project.root)), decl.line + 1, decl.kind, decl.name)
                )
    assert not missing, f"{len(missing)} declarations not indexed; first: {missing[:15]}"


def _cross_file_calls(project: Project) -> list[tuple[gt.CallSite, str, WorkspaceSymbol]]:
    """Deterministic sample of `jsr NAME` sites whose target has exactly one
    definition in another file."""
    if _contaminated(project):
        pytest.xfail(
            "index contains tool-directory copies; see test_tool_directories_are_not_indexed"
        )
    picks = []
    for path in project.files:
        if gt.is_acme(path):
            continue
        for call in gt.call_sites(path):
            # Collapse byte-identical records so the double-emission bug
            # (test_no_symbol_is_emitted_twice) does not hide other defects.
            defs = list(
                {
                    s
                    for s in project.index.lookup(call.name)
                    if s.kind in _DEFINITION_KINDS and not s.scope_path
                }
            )
            if len(defs) != 1 or defs[0].uri == project.uri(path):
                continue
            picks.append((call, project.uri(path), defs[0]))
    # Spread the sample over the whole project rather than the first files.
    step = max(1, len(picks) // _SAMPLE)
    return picks[::step][:_SAMPLE]


def test_cross_file_calls_have_a_target(project: Project):
    if not any(gt.call_sites(p) for p in project.files if not gt.is_acme(p)):
        pytest.skip("project has no jsr/jmp call sites (headers only)")
    assert _cross_file_calls(project), (
        "no cross-file call sites found; the oracle or the index is broken"
    )


def test_definition_from_call_site_resolves_to_the_proc(project: Project):
    wrong = []
    for call, uri, target in _cross_file_calls(project):
        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=call.line, character=call.column + 1),
        )
        result = srv.on_definition(project.server, params) or []
        hits = [
            loc
            for loc in result
            if loc.uri == target.uri and loc.range.start.line == target.selection_range.start.line
        ]
        if not hits:
            wrong.append(
                (
                    project.path(uri).name,
                    call.line + 1,
                    call.name,
                    [(project.path(loc.uri).name, loc.range.start.line + 1) for loc in result],
                )
            )
    assert not wrong, (
        f"{len(wrong)} call sites did not resolve to their definition; first: {wrong[:10]}"
    )


def test_references_from_definition_include_the_call_site(project: Project):
    missing = []
    for call, uri, target in _cross_file_calls(project):
        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri=target.uri),
            position=lsp.Position(
                line=target.selection_range.start.line,
                character=target.selection_range.start.character + 1,
            ),
            context=lsp.ReferenceContext(include_declaration=False),
        )
        result = srv.on_references(project.server, params) or []
        if not any(loc.uri == uri and loc.range.start.line == call.line for loc in result):
            missing.append((target.name, project.path(uri).name, call.line + 1, len(result)))
    assert not missing, f"{len(missing)} call sites absent from references; first: {missing[:10]}"


def test_references_do_not_leak_across_unrelated_names(project: Project):
    """A reference to `foo` must never be attributed to `foobar` or `foo2`."""
    bad = []
    for _, _, target in _cross_file_calls(project)[:20]:
        for ref in project.index.references(target.name):
            text = project.lines(project.path(ref.uri))[ref.range.start.line][
                ref.range.start.character : ref.range.end.character
            ]
            if text != target.name:
                bad.append(
                    (target.name, project.path(ref.uri).name, ref.range.start.line + 1, text)
                )
    assert not bad, f"{len(bad)} mis-attributed references; first: {bad[:10]}"


def test_workspace_symbol_search_finds_every_proc(project: Project):
    procs = [s for s in _all(project) if s.kind == SymbolKind.PROC][
        :: max(1, len(_all(project)) // 200)
    ]
    missing = [
        s.name
        for s in procs
        if not any(hit.name == s.name for hit in project.index.search(s.name, limit=50))
    ]
    assert not missing, f"{len(missing)} procs not found by search; first: {missing[:10]}"
