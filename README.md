# ca65-asm-serena-lsp

A language server for **CA65 assembly** (the [cc65](https://cc65.github.io/) toolchain's assembler dialect for 6502/65C02/65816 targets — NES, C64, Apple II, Atari), plus the [Serena MCP](https://github.com/oraios/serena) integration that exposes it to coding agents.

Gives Serena's symbolic tools (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `rename_symbol`, hover) real understanding of CA65 source: labels, procs, scopes, macros, structs, imports/exports, and includes.

## Status

**v1 complete and in daily use.** Validated end-to-end against a real C64 project of 247 sources / 8.6k symbols (cold reindex 1.3s, `.dbg` enrichment active), and regression-tested against ten real CA65 codebases by the [red/green gate](docs/red-green-gate.md).

Upstreaming into `oraios/serena` was declined ([oraios/serena#1504](https://github.com/oraios/serena/pull/1504), closed 2026-05-26 as too niche / third-party-dependency risk), so the integration lives permanently in the fork [`JC-000/serena@feature/ca65-language-server`](https://github.com/JC-000/serena/tree/feature/ca65-language-server), kept rebased onto upstream main.

## Correctness

Every change to the language server, the Serena shim, or the Bash nudge hook goes through `scripts/gate.sh`, which runs five layers: the hook suite, ca65-ls unit tests, a contract suite against ten real `c64-*` projects, a through-Serena layer driven via SolidLSP, and the fork's end-to-end tests.

The corpus expectations are derived from the source text rather than hand-written, so they cannot rot as those projects evolve, and the suite carries oracles that do not share code with what they check. Known defects are recorded as strict `xfail` tests: a fix makes one pass unexpectedly and fails the gate until the marker is removed, so the suite always states what is still owed. See [`docs/red-green-gate.md`](docs/red-green-gate.md) for the convention and the full findings history.

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
- `scripts/gate.sh [--quick]` — the red/green gate; run it before and after any change to the tools.
- `scripts/ca65_bash_nudge.py` — an optional Claude Code hook that nudges assembly work off `grep`/`sed` and onto the symbolic tools (see below).
- `docs/red-green-gate.md` — the testing convention and the defect history.
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

### Per-project language config

The CA65 language server only starts for projects whose `.serena/project.yml` lists it:

```yaml
language_servers:
- python
- ca65
```

(Serena v1.7.0 renamed the key from `languages:`; old configs still auto-migrate, but if a file carries **both** keys the old one is silently ignored, so keep exactly one.)

Without that entry the MCP activates fine but every symbolic tool silently runs without the CA65 backend — the most likely cause of "the CA65 tools aren't being used". Serena loads project configs once at MCP startup, so after editing a `project.yml`, restart Claude Code; re-activating the project or calling `restart_language_server` does not reload it.

### Claude Code hooks

Serena ships Claude Code hooks (`serena-hooks activate|remind|auto-approve|cleanup`, wired in `~/.claude/settings.json`). The stock install runs them from the PyPI `serena-agent` package, whose reminder hook does not recognize assembly sources. The fork adds `.s`/`.inc`/`.asm`/`.mac` to the reminder's code-file extensions; to get that behavior, point the hook commands at the fork checkout's entrypoint instead:

```sh
# instead of: uvx --from serena-agent serena-hooks <cmd> --client=claude-code
~/Documents/serena/.venv/bin/serena-hooks <cmd> --client=claude-code
```

The fork venv's install is editable, so hook changes there take effect immediately — but the hooks now depend on that venv existing.

### The Bash nudge hook (optional)

Serena's own reminder hook is blind to `Bash` on Claude Code: it matches the native `Read` and `Grep` tool *names*, while real CA65 sessions read source almost entirely through shell commands (one measured session ran 203 `Bash` calls and a single `Read`). `scripts/ca65_bash_nudge.py` is a `PreToolUse` hook that closes that gap without touching Serena: after three consecutive shell reads or greps of assembly files **inside a project that has the ca65 backend enabled**, it denies once and names the symbolic tool that would have answered.

It is deliberately narrow, because a false positive blocks real work: it parses the command with a shell-aware tokenizer (heredocs, quotes, newlines, every operator, `cd` and `for` bindings), and counts a read only when it has an assembly operand inside the project that owns the working directory, or is a recursive grep rooted in a project directory that holds assembly. Wire it in `~/.claude/settings.json` with an empty matcher, alongside the Serena hooks, so it sees both `Bash` and `mcp__serena__` calls.

### Agents working in git worktrees

A worktree checkout under `.claude/worktrees/` has no `.serena/` directory of its own, so an agent working there should activate the **worktree path** as its project to get a correct, single-copy index of its own branch. Activating the parent project instead indexes the parent checkout, not the agent's edits. Serena memories are unaffected either way. The indexer skips nested checkouts, so the worktree copies never pollute the parent's index.

## PyPI

The former blocker (a git-URL dependency on `tree-sitter-ca65`, which PyPI rejects) is resolved: the grammar is vendored into `packages/ca65-ls/vendor/tree-sitter-ca65/` (pinned `b22ead1`, MIT — see the NOTICE.md there) and compiled into the wheel as an abi3 C extension. First release procedure: `RELEASING.md`.
