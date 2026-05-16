# M1 Spike — Findings

Exit criterion: a standalone parser turns cc65 `.dbg` files into a queryable symbol index, validated against the synthetic test_repo and a real cc65 sample. **Met.**

## What works

- `ca65_ls/index/dbg_oracle.py` parses every record type observed in cc65 v2.18 `.dbg` output: `version`, `info`, `file`, `line`, `mod`, `seg`, `span`, `scope`, `sym`. Quoted strings, hex (`0x…`), decimals, and `+`-separated id lists all round-trip cleanly.
- Scope-path resolution walks the `sym.scope → parent → …` chain and produces tuples like `("helpers", "foo")` for `helpers::foo`.
- Cheap locals are correctly distinguished by their `parent` sym — e.g. `@inner` in `helpers::foo` and `@inner` in `helpers::bar` resolve to distinct records via `parent_name`.
- Cross-module imports (`type=imp`) are linked back to their exporting symbol via the `exp` field → `import_of` in our records.
- 14 unit tests pass against the synthetic fixture; the `_finds_every_declared_export` test asserts ≥95% coverage and lands at 100% on the synthetic corpus.

## Surprises / important constraints discovered

1. **`a` is a reserved word in CA65** (the accumulator register name) and cannot be used as a struct field name. Cost us one debug cycle. Worth a diagnostic in the LSP eventually: "field name `a` shadows accumulator — use a different name".

2. **`.export name := $1234` is recorded as `type=lab`, not `type=equ`.** cc65 treats any export-with-resolved-value as an absolute-address label. The semantic distinction (label vs equate) at the `.dbg` level is the presence of a `seg=N` field, not the `type=` tag. The oracle's `SymbolRecord.kind` reflects raw cc65 type; consumers wanting the "is this a constant" answer should check `addrsize == "absolute" and segment is None`.

3. **Library archives do NOT contribute sym records to the `.dbg`.** The cc65 `hello.c` sample built with `cc65 --debug-info && ca65 -g && ld65 --dbgfile` yields a 108KB `.dbg` with only 28 sym records, even though the resulting `.lbl` has 131 names. The other 103 come from `c64.lib` (KERNAL entry points, runtime helpers) and are resolved at link time but never decomposed into source-line debug info. The `info lib=1` record tells you a library was linked — there's a single `lib id=0,name=".../c64.lib"` line — but no further detail.

   **Consequence for the LSP:** when the user does `find_symbol("BSOUT")` and BSOUT is a KERNAL routine, we need a fallback path. Options: (a) ship a bundled symbol map of common KERNAL/Kernal-extension entry points, (b) parse the `.lbl` post-link to recover addresses without source locations, (c) accept the limit and refer the user to the C64 Programmer's Reference Guide. (a) + (b) is probably the right combo for M4.

4. **`.dbg` semantics for source position are *def-line* based**, not range-based. A `sym` record points at line ids via `def=<id>[+<id>...]`; each line id maps to (file_id, line_no). There's no character-column info. Tree-sitter ranges will be richer when we add them in M2.

5. **Compiler-generated labels** like `L0007`, `L002C` (cc65 emits these for branch targets inside C functions) appear as `type=lab` symbols. They have addresses but no real "name" the user would search for. The Indexer should probably filter these out from `workspace/symbol` results by default (regex `^L[0-9A-F]+$`) but keep them in the index so `find_referencing_symbols` on a numeric address still works.

## Round-trip cross-validation against `.lbl`

For every source-defined label in the cc65 `hello.c` `.dbg` (those with `type=lab` and a resolved `val=`), the address matches the `.lbl` file 100% (4/4). This is the metric the Indexer's CI breadth-test should use, *not* "fraction of .lbl names found in .dbg" — the latter is bounded above by what's source-defined vs library-defined.

## Deliverables produced

- `packages/ca65-ls/ca65_ls/index/dbg_oracle.py` — parser + DbgIndex + CLI
- `packages/ca65-ls/tests/test_dbg_oracle.py` — 14 tests, all passing
- `packages/ca65-ls/tests/fixtures/test_repo/` — synthetic CA65 corpus + committed `.dbg`/`.lbl`/`.map` fixtures
- `packages/ca65-ls/tools/regen_fixtures.sh` — rebuilds the fixtures after corpus edits
- `pyproject.toml` with pygls, tree-sitter (generic, no grammar yet), pytest, ruff

## Open questions handed off to M2

- Which tree-sitter-ca65 grammar will we use? `babasbot/tree-sitter-ca65` returns 404. The Researcher agent is surveying alternatives — block on its output before adding a grammar dep.
- How will the Indexer reconcile tree-sitter symbol locations (character ranges) with the `.dbg`'s line-only locations? Probably: tree-sitter for ranges in the editor, `.dbg` cross-checks for "is this exactly the right symbol after linking".
