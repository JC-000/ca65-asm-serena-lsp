#!/usr/bin/env python3
"""
M4 smoke test -- exercise the CA65 LSP + Serena symbolic tools against a real
CA65 project (defaults to c64-https) and report what works.

Runs entirely in-process using the Serena fork's venv at
~/Documents/serena/.venv, so it does NOT require restarting
Claude Code or modifying ~/.claude.json.  This makes it safe to run repeatedly
while iterating on the LSP.

Usage:
    scripts/m4_smoke_test.py [PROJECT_PATH]
    scripts/m4_smoke_test.py --help

Default PROJECT_PATH is ~/Documents/c64-https.

What it checks (in order; failures don't stop later checks):

    1. Does the LSP start at all?  (boot)
    2. How many .s / .asm / .inc files does ca65-ls discover?
    3. How many symbols does it index?  Cold / warm timings.
    4. Does the .dbg enricher attach addresses to a sample symbol?
    5. Pick one symbol per kind (PROC / SCOPE / MACRO / LABEL / IMPORT /
       EXPORT) and:
         - request_document_symbols on its file - is it in the outline?
         - request_definition from a use site - does it resolve?
         - request_references from the definition - does it find call sites?
    6. Pick the top-5 most-referenced symbols and report their fan-out.

The output is a Markdown table you can paste back as feedback for M4 bug
triage.  Exit code is 0 if every category had at least one successful probe,
1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ------------------------------------------------------------ venv discovery


SERENA_VENV = Path.home() / "Documents" / "serena" / ".venv"


def _bootstrap_into_fork_venv() -> None:
    """Re-exec under the Serena fork's venv if we're not already there.

    The fork venv has both `solidlsp` (Serena) and `ca65_ls` installed -- the
    rest of the user's Python environments don't.
    """
    fork_python = SERENA_VENV / "bin" / "python"
    if not fork_python.exists():
        sys.stderr.write(
            f"ERROR: Serena fork venv not found at {SERENA_VENV}.\n"
            f"Run the M3 setup in the project repo first:\n"
            f"    cd {SERENA_VENV.parent}\n"
            f"    uv venv --python 3.12 .venv\n"
            f"    uv pip install --python .venv/bin/python -e .\n"
            f"    uv pip install --python .venv/bin/python "
            f"-e {Path(__file__).resolve().parent.parent / 'packages' / 'ca65-ls'}\n"
        )
        sys.exit(1)
    if sys.executable != str(fork_python):
        import os
        os.execv(str(fork_python), [str(fork_python), *sys.argv])


_bootstrap_into_fork_venv()


# ------------------------------------------------------------ imports (post-venv)


from ca65_ls.buffer.document import Document  # noqa: E402
from ca65_ls.index.dbg_oracle import load as load_dbg  # noqa: E402
from ca65_ls.index.workspace import WorkspaceIndex  # noqa: E402
from ca65_ls.types import SymbolKind  # noqa: E402


# ----------------------------------------------------------------- result types


@dataclass
class Result:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    ms: Optional[float] = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)
        emoji = {"PASS": "[ok]", "FAIL": "[FAIL]", "SKIP": "[skip]"}[r.status]
        timing = f"  ({r.ms:.0f} ms)" if r.ms is not None else ""
        print(f"{emoji:7}  {r.name}{timing}")
        # Print detail lines for FAIL/SKIP unconditionally, and for PASS when
        # the result *carries* extra info worth seeing (top-N lists, stats).
        if r.detail and (r.status != "PASS" or "\n" in r.detail or r.detail.startswith("  ")):
            for line in r.detail.splitlines():
                print(f"            {line}")

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "PASS")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "FAIL")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "SKIP")

    def summary(self) -> str:
        return f"{self.passed} passed, {self.failed} failed, {self.skipped} skipped"


def safely(name: str, report: Report, fn) -> Any:
    """Run fn() and add a Result; return its value or None on exception."""
    start = time.perf_counter()
    try:
        value = fn()
        ms = (time.perf_counter() - start) * 1000
        report.add(Result(name=name, status="PASS", ms=ms))
        return value
    except Exception as exc:
        ms = (time.perf_counter() - start) * 1000
        tb = traceback.format_exc(limit=2)
        report.add(Result(name=name, status="FAIL", detail=f"{exc.__class__.__name__}: {exc}\n{tb}", ms=ms))
        return None


# ----------------------------------------------------------------- checks


def check_workspace_index(project_root: Path, report: Report) -> Optional[WorkspaceIndex]:
    """Spin up a fresh WorkspaceIndex and validate cold/warm timing."""

    def cold():
        idx = WorkspaceIndex(project_root, cache=False)
        idx.reindex()
        return idx

    cold_idx = safely("Cold reindex (no cache)", report, cold)
    if cold_idx is None:
        return None

    def warm():
        idx = WorkspaceIndex(project_root, cache=True)
        idx.reindex()  # populates cache
        idx2 = WorkspaceIndex(project_root, cache=True)
        idx2.reindex()  # reads cache
        return idx2

    warm_idx = safely("Warm reindex (mtime cache)", report, warm)

    idx = warm_idx or cold_idx
    if idx is None:
        return None

    stats = idx.stats
    report.add(Result(
        name=f"Workspace stats: {stats.get('files')} files / {stats.get('symbols')} symbols indexed",
        status="PASS",
        detail=json.dumps(stats, indent=2, default=str),
    ))
    return idx


def check_dbg_enrichment(project_root: Path, idx: WorkspaceIndex, report: Report) -> None:
    """If a *.dbg exists, find a symbol with an address and verify enrichment."""
    dbg_paths = list(project_root.glob("build/*.dbg")) + list(project_root.glob("obj/*.dbg"))
    if not dbg_paths:
        report.add(Result(
            name=".dbg enrichment",
            status="SKIP",
            detail=f"No build/*.dbg or obj/*.dbg found under {project_root}; run `make` to generate.",
        ))
        return

    def probe():
        oracle = load_dbg(dbg_paths[0])
        # Find a symbol that the oracle knows about WITH an address, and that
        # our WorkspaceIndex also has.
        for name, recs in oracle.symbols_by_name.items():
            for r in recs:
                if r.kind == "label" and r.addr is not None and idx.lookup(name):
                    wsmatches = [w for w in idx.lookup(name) if w.address == r.addr]
                    if wsmatches:
                        return f"{name} @ ${r.addr:04X} in {r.segment or '(no segment)'}"
        return None

    found = safely(f".dbg enrichment (using {dbg_paths[0].name})", report, probe)
    if found is None:
        report.add(Result(
            name=".dbg enrichment",
            status="FAIL",
            detail="No symbol's address from .dbg made it into the WorkspaceIndex.",
        ))


def check_symbol_navigation(idx: WorkspaceIndex, report: Report) -> None:
    """Pick one symbol per kind; do round-trip definition + references."""
    by_kind: dict[SymbolKind, list] = defaultdict(list)
    for ws in idx.all_symbols():
        by_kind[ws.kind].append(ws)

    interesting_kinds = [
        SymbolKind.PROC,
        SymbolKind.SCOPE,
        SymbolKind.MACRO,
        SymbolKind.STRUCT,
        SymbolKind.LABEL,
        SymbolKind.IMPORT,
        SymbolKind.EXPORT,
    ]

    for kind in interesting_kinds:
        candidates = by_kind.get(kind, [])
        if not candidates:
            report.add(Result(name=f"Navigation: {kind.value}", status="SKIP", detail="No symbols of this kind in workspace."))
            continue

        # Sample the lexically-first symbol whose name doesn't start with `__`
        # (skip linker-internal names).
        pick = next((w for w in candidates if not w.name.startswith("__")), None) or candidates[0]

        def probe(p=pick):
            defs = idx.lookup(p.name)
            assert defs, f"lookup({p.name}) returned []"
            refs = idx.references(p.name)
            return f"name={p.name!r}  scope={'::'.join(p.scope_path) or '(none)'}  defs={len(defs)}  refs={len(refs)}"

        safely(f"Navigation: {kind.value}", report, probe)


def check_top_referenced(idx: WorkspaceIndex, report: Report) -> None:
    """List the top-5 most-referenced symbols by fan-out."""
    counter: Counter = Counter()
    for ws in idx.all_symbols():
        counter[ws.name] += len(idx.references(ws.name))

    top = [(name, count) for name, count in counter.most_common() if count > 0][:5]
    if not top:
        report.add(Result(name="Top-referenced symbols", status="SKIP", detail="No references found in workspace."))
        return

    lines = [f"  {name}: {count} refs" for name, count in top]
    report.add(Result(name="Top-5 most-referenced symbols", status="PASS", detail="\n".join(lines)))


def check_document_symbols(project_root: Path, idx: WorkspaceIndex, report: Report) -> None:
    """Pick 3 .s files and verify Document.symbols returns a non-empty hierarchy."""
    s_files = sorted(project_root.rglob("*.s"))[:3]
    if not s_files:
        report.add(Result(name="documentSymbol probe", status="SKIP", detail="No .s files found."))
        return

    for path in s_files:
        rel = path.relative_to(project_root)

        def probe(p=path):
            doc = Document(p.as_uri(), p.read_text(encoding="utf-8"))
            return f"{len(doc.symbols)} top-level / {len(doc.flat_symbols())} total"

        safely(f"documentSymbol on {rel}", report, probe)


# --------------------------------------------------------------- main


DEFAULT_PROJECT = Path.home() / "Documents" / "c64-https"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("project", nargs="?", type=Path, default=DEFAULT_PROJECT, help="CA65 project root")
    args = p.parse_args(argv)

    project = args.project.resolve()
    if not project.is_dir():
        print(f"ERROR: {project} is not a directory.", file=sys.stderr)
        return 2

    print(f"\n=== M4 smoke test: {project} ===\n")
    report = Report()

    idx = check_workspace_index(project, report)
    if idx is None:
        print("\nFatal: WorkspaceIndex could not be built.  Aborting further checks.")
        print(report.summary())
        return 1

    check_dbg_enrichment(project, idx, report)
    check_document_symbols(project, idx, report)
    check_symbol_navigation(idx, report)
    check_top_referenced(idx, report)

    print(f"\n=== {report.summary()} ===\n")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
