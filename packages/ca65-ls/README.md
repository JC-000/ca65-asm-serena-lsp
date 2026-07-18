# ca65-ls

A language server for **CA65** assembly (the macro assembler from the [cc65](https://cc65.github.io/) toolchain for 6502/65C02/65816 targets).

Designed to plug into [Serena MCP](https://github.com/oraios/serena) so its symbolic agent tools (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `rename_symbol`) work against CA65 codebases — but it speaks plain LSP over stdio, so any LSP client can drive it.

## Status

**Beta.** Implements document symbols, workspace symbols, go-to-definition, scope-aware find-references, hover, and rename. Validated end-to-end against real C64 CA65 projects. Not yet published to PyPI — see [`../../RELEASING.md`](../../RELEASING.md) for install-from-git instructions and the packaging plan.

## Capabilities

| LSP request | Notes |
|---|---|
| `textDocument/documentSymbol` | Labels, procs, macros, scopes, structs, enums, imports/exports. |
| `workspace/symbol` | Lazy, cached, project-wide index. |
| `textDocument/definition` | Scope-aware. |
| `textDocument/references` | Scope-aware — same-named cheap-locals (`@loop`, `@done`) in different routines don't collide. |
| `textDocument/hover` | Symbol kind, `.dbg`-derived address/segment info when available, leading comment block. |
| `textDocument/rename` + `prepareRename` | Scope-aware edits across the workspace. |

## Architecture (in brief)

- **Buffer layer** — `tree-sitter-ca65` parses every open document; we derive scopes, cheap-locals, anonymous-label sites.
- **Project index** — workspace-wide symbol table, fed primarily from tree-sitter and enriched (when present) by `ld65 --dbgfile` debug info, `-Ln` label files, and `-m` map files.
- **Diagnostics** — `ca65 -g` subprocess on save.

The hybrid: tree-sitter is the source of truth for positional facts (definitions, references, rename); cc65 debug-info is the source of truth for address-y facts (segment layout, archive-symbol provenance). tree-sitter runs unconditionally, so navigation works even before a project is built; the debug info is an opt-in enricher.

## Install

From git (until PyPI publication is unblocked):

```sh
pip install "ca65-ls @ git+https://github.com/JC-000/ca65-asm-serena-lsp@main#subdirectory=packages/ca65-ls"
ca65-ls --stdio
```

For development:

```sh
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## Layout

```
ca65_ls/
  server.py            # pygls entry point + request router
  buffer/              # tree-sitter Document + scope/cheap-local/anon-label resolution
  index/               # project-level symbol index
    workspace.py
    dbg_oracle.py      # independent .dbg parser; also used as test ground truth
tests/                 # pytest harness (does not require Serena)
tools/
  regen_fixtures.sh    # regenerates the synthetic-corpus debug fixtures
```

## License

[MIT](../../LICENSE).
