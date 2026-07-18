# ca65-asm-serena-lsp

A language server for **CA65** assembly — the macro assembler from the
[cc65](https://cc65.github.io/) toolchain for 6502/65C02/65816 targets (NES,
C64, Apple II, Atari, and friends) — plus the glue that plugs it into the
[Serena MCP](https://github.com/oraios/serena) toolkit.

The goal: give Serena's symbolic code-navigation tools
(`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`,
`rename_symbol`, …) real understanding of CA65 source, so an agent can navigate
labels, macros, procs, scopes, and imports/exports the same way it does in a
Python or TypeScript codebase.

## What's here

This repo is a small monorepo:

- **[`packages/ca65-ls/`](packages/ca65-ls/)** — `ca65-ls`, a standalone Python
  LSP daemon (built on [pygls](https://github.com/openlawlibrary/pygls) and
  [tree-sitter-ca65](https://github.com/pogyomo/tree-sitter-ca65)). It works
  with any LSP client, not just Serena.
- **[`docs/research/`](docs/research/)** — the grammar-coverage matrix
  (`ts-ca65-coverage.md`) and the cc65 `.dbg` debug-format spec
  (`dbg-format.md`) the indexer is built against.
- **[`scripts/`](scripts/)** — helpers for wiring the server into a local Serena
  checkout and for smoke-testing against real CA65 projects.

The Serena-side integration (a `Ca65LanguageServer` shim, a `Language.CA65`
enum entry, and end-to-end tests) lives in a fork of Serena and is proposed
upstream in **[oraios/serena#1504](https://github.com/oraios/serena/pull/1504)**.

## Features

`ca65-ls` currently implements:

- **Document symbols** — labels, procs, macros, scopes, structs, enums,
  imports/exports.
- **Workspace symbols** — a lazy, cached, project-wide symbol index.
- **Go to definition** and **find references** — scope-aware, so same-named
  cheap-locals (`@loop`, `@done`) in different routines don't collide.
- **Hover** — symbol kind, `.dbg`-derived address/segment info when available,
  and the leading comment block.
- **Rename** (with `prepareRename`) — scope-aware edits across the workspace.

See the [package README](packages/ca65-ls/README.md) for the design details.

## How it works

`ca65-ls` is a hybrid indexer:

- **tree-sitter-ca65 is the primary source of truth** for positional facts
  (definitions, references, scopes, rename edits). It runs on every open
  buffer, so navigation works even for an unbuilt project.
- **cc65 debug info is an optional enricher.** When a build passes
  `ld65 --dbgfile` (plus `-Ln`/`-m`), the `.dbg`/`.lbl`/`.map` outputs add
  addresses, segment layout, and archive-symbol provenance. When those files
  aren't present — the common case — the server degrades gracefully to
  tree-sitter-only.

```
   +-----------------+      LSP / JSON-RPC     +---------------------------+
   |   LSP client    |  <------ stdio ------->  |   ca65-ls (Python+pygls)  |
   | (Serena, editor)|                          |                           |
   +-----------------+                          |  tree-sitter-ca65  (AST)  |
                                                |         +  .dbg/.lbl/.map  |
                                                |            (enrichment)    |
                                                +---------------------------+
```

## Install

`ca65-ls` isn't on PyPI yet (its tree-sitter grammar dependency isn't either —
see [RELEASING.md](RELEASING.md)). Install it from git:

```sh
pip install "ca65-ls @ git+https://github.com/JC-000/ca65-asm-serena-lsp@main#subdirectory=packages/ca65-ls"
```

Then run it over stdio:

```sh
ca65-ls --stdio
```

Point any LSP client that speaks stdio at that command, associating it with
`.s`/`.inc` CA65 files.

## Use with Serena

Until the upstream PR lands, use the Serena fork that carries the CA65
integration. The [`scripts/install_local_serena.sh`](scripts/install_local_serena.sh)
helper rewrites your Claude Code MCP config to point at a local Serena checkout
with `ca65-ls` installed (it backs up the previous config and prints revert
instructions). Once
[oraios/serena#1504](https://github.com/oraios/serena/pull/1504) is merged,
upstream Serena will recognize CA65 projects directly.

## Development

Everything below runs from `packages/ca65-ls/`.

```sh
# One-time: create the venv and install with dev extras.
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"

# Run the test suite (unit + integration; no Serena required).
.venv/bin/python -m pytest -q

# Run the server standalone for ad-hoc smoke testing.
.venv/bin/ca65-ls --stdio

# Dump a .dbg file as JSON (debugging aid).
.venv/bin/ca65-dbg-dump path/to/file.dbg
```

Test fixtures under `packages/ca65-ls/tests/fixtures/test_repo/` are a small
synthetic CA65 corpus; the committed `.dbg`/`.lbl`/`.map` build artifacts are
regenerated with `tools/regen_fixtures.sh` (needs the cc65 toolchain on
`PATH`).

## Releasing

Packaging and PyPI-publication plans — including the tree-sitter-ca65
dependency blocker and the fallback of vendoring the grammar — are documented
in [RELEASING.md](RELEASING.md).

## License

[MIT](LICENSE).
