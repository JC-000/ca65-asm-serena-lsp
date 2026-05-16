# ca65-ls

A language server for **CA65** assembly (the macro assembler from the [cc65](https://cc65.github.io/) toolchain for 6502/65C02/65816 targets).

Designed to plug into [Serena MCP](https://github.com/oraios/serena) so its symbolic agent tools (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `rename_symbol`) work against CA65 codebases.

## Status

**Pre-alpha.** See `/Users/someone/.claude/plans/there-are-no-lsp-s-drifting-rain.md` for the project plan.

## Architecture (in brief)

- **Buffer layer** — `tree-sitter-ca65` parses every open document; we derive scopes, cheap-locals, anonymous-label sites.
- **Project index** — workspace-wide symbol table, fed primarily from tree-sitter and enriched (when present) by `ld65 --dbgfile` debug info, `-Ln` label files, and `-m` map files.
- **Diagnostics** — `ca65 -g` subprocess on save.

The hybrid: tree-sitter is the source of truth for positional facts (definitions, references, rename); cc65 debug-info is the source of truth for address-y facts (segment layout, archive-symbol provenance).

## Install (development)

```sh
uv pip install -e ".[dev]"
ca65-ls --help
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
