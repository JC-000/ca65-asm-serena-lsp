# ca65-asm-serena-lsp

A language server for **CA65 assembly** (the [cc65](https://cc65.github.io/) toolchain's assembler dialect for 6502/65C02/65816 targets — NES, C64, Apple II, Atari), plus the [Serena MCP](https://github.com/oraios/serena) integration that exposes it to coding agents.

Gives Serena's symbolic tools (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `rename_symbol`, hover) real understanding of CA65 source: labels, procs, scopes, macros, structs, imports/exports, and includes.

## Status

**v1 complete and in daily use.** Validated end-to-end against a real 224-file / ~12k-symbol C64 project (cold reindex ~1.2s, `.dbg` enrichment active).

Upstreaming into `oraios/serena` was declined ([oraios/serena#1504](https://github.com/oraios/serena/pull/1504), closed 2026-05-26 as too niche / third-party-dependency risk), so the integration lives permanently in the fork [`JC-000/serena@feature/ca65-language-server`](https://github.com/JC-000/serena/tree/feature/ca65-language-server), kept rebased onto upstream main.

## Architecture

```
   +-----------------+      LSP/JSON-RPC      +---------------------------+
   |   Serena MCP    |  <------ stdio ------> |   ca65-ls (Python+pygls)  |
   |  (SolidLS shim) |                        |   server.py               |
   +-----------------+                        |   |                       |
      (fork branch)                           |   v                       |
                                              |  +--------+ +-----------+ |
                                              |  | Buffer | | Project   | |
                                              |  | layer  | | index     | |
                                              |  | (TS)   | | (lazy)    | |
                                              |  +---+----+ +-----+-----+ |
                                              +------|-----------|--------+
                                                  tree-sitter   .dbg/.lbl/
                                                  -ca65 AST     .map
                                                  (PRIMARY)     (ENRICHER)
```

- **[tree-sitter-ca65](https://github.com/pogyomo/tree-sitter-ca65)** parses source and is the primary source of truth — the LSP works on any CA65 codebase with no build required.
- **cc65 debug info** (`.dbg`, when the project links with `ld65 --dbgfile`) is an opt-in enricher: post-link addresses, segment membership, symbol sizes.

## Layout

- `packages/ca65-ls/` — the standalone LSP daemon (Python, pygls). Tests, fixtures, and a `.dbg`-dump debug tool included; see `CLAUDE.md` for dev commands.
- `scripts/install_local_serena.sh` — points Claude Code's `serena` MCP entry at the fork + this ca65-ls source (project-scoped by default, `--global` optional).
- `scripts/m4_smoke_test.py [PROJECT]` — exercises the LSP against a real CA65 project and prints a report.
- `docs/research/` — grammar coverage matrix and `.dbg` format spec.

## Install

```sh
# one-time: wire Claude Code's serena MCP to the CA65-aware fork
scripts/install_local_serena.sh            # scoped to one project
scripts/install_local_serena.sh --global   # or everywhere
# then restart Claude Code
```

After the fork branch is updated (e.g. a rebase onto upstream), refresh the cached build:

```sh
uvx --refresh --from "git+https://github.com/JC-000/serena@feature/ca65-language-server" \
    --with-editable "$(pwd)/packages/ca65-ls" serena --help
```

`ca65-ls` itself is installed `--with-editable`, so local source changes take effect on the next MCP restart with no reinstall.

## PyPI

The former blocker (a git-URL dependency on `tree-sitter-ca65`, which PyPI rejects) is resolved: the grammar is vendored into `packages/ca65-ls/vendor/tree-sitter-ca65/` (pinned `b22ead1`, MIT — see the NOTICE.md there) and compiled into the wheel as an abi3 C extension. First release procedure: `RELEASING.md`.
