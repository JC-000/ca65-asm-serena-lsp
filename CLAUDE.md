# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical sources (read these first)

The README is the public overview (refreshed 2026-07-20). Deeper project state lives in:

1. **`~/.claude/plans/there-are-no-lsp-s-drifting-rain.md`** — the approved implementation plan, with milestones M1–M5, architecture, agent-team plan, and risk register.
2. **`~/.claude/projects/-Users-someone-Documents-ca65-asm-serena-lsp/memory/MEMORY.md`** — auto-memory index pointing to canonical project notes managed by Serena (run `mcp__serena__list_memories` then `mcp__serena__read_memory`).
3. **`docs/research/ts-ca65-coverage.md`** + **`docs/research/dbg-format.md`** — the Researcher agent's directive-by-directive grammar coverage matrix and `.dbg` format spec. Reference material the Indexer and Parser layers depend on.
4. **`packages/ca65-ls/docs/m1-spike.md`** — surprises and constraints discovered during M1 (esp. that library archives like `c64.lib` don't contribute sym records to the `.dbg`).

## Two-repo layout

This project spans two checkouts that must stay in sync:

- **`~/Documents/ca65-asm-serena-lsp/`** (this repo, GitHub: `JC-000/ca65-asm-serena-lsp`, private) — the standalone `ca65-ls` Python LSP daemon under `packages/ca65-ls/`, plus research docs, scripts, and the approved plan.
- **`~/Documents/serena/`** (GitHub: `JC-000/serena`, fork of `oraios/serena`) — the Serena integration: `Ca65LanguageServer` shim, `Language.CA65` enum entry, factory case, test corpus, and the `test/solidlsp/ca65/test_ca65_basic.py` end-to-end tests. Lives permanently on branch `feature/ca65-language-server` — upstreaming was declined (see Milestone status), so the fork is the long-term home and is periodically rebased onto `oraios/serena` main.

When changing the buffer/index/server interfaces, **both repos need updates**: the LSP server's behavior in this repo, and Serena's test assertions in the fork.

## Architecture (3 layers, pinned interface)

```
   +-----------------+      LSP/JSON-RPC      +---------------------------+
   |   Serena MCP    |  <------ stdio ------> |   ca65-ls (Python+pygls)  |
   |  (SolidLS shim) |                        |   server.py                |
   +-----------------+                        |   |                        |
                                              |   v                        |
                                              |  +--------+ +-----------+  |
                                              |  | Buffer | | Project   |  |
                                              |  | layer  | | index     |  |
                                              |  | (TS)   | | (lazy)    |  |
                                              |  +---+----+ +-----+-----+  |
                                              +------|-----------|---------+
                                                  tree-sitter   .dbg/.lbl/
                                                  -ca65 AST     .map
                                                  (PRIMARY)     (ENRICHER)
                                                                ca65 -g
                                                                (diagnostics)
```

The three layers were built by parallel agents against a **pinned data contract** in `packages/ca65-ls/ca65_ls/types.py` (`BufferSymbol`, `WorkspaceSymbol`, `SymbolReference`). Treat `types.py` as a stable interface — changing it breaks both the Parser and Indexer in lockstep.

**Key architectural insight (and a correction to the original plan):** tree-sitter-ca65 is the **primary source of truth**, not the cc65 debug info. The `.dbg` file is an opt-in enricher (addresses, segments, archive-symbol provenance) when `ld65 --dbgfile` is set; the LSP degrades gracefully to tree-sitter-only when not. Reason: real CA65 builds (including the user's c64-https) don't pass `--dbgfile` by default. See `packages/ca65-ls/docs/m1-spike.md`.

**Tree-sitter grammar pin:** `pogyomo/tree-sitter-ca65 @ b22ead1`, vendored under `packages/ca65-ls/vendor/tree-sitter-ca65/` (no external grammar dependency). The earlier `babasbot/tree-sitter-ca65` referenced in pre-M1 research is 404. Five grammar gaps documented in `docs/research/ts-ca65-coverage.md`.

## Commands

All from `packages/ca65-ls/` unless noted. The venv lives at `packages/ca65-ls/.venv/`.

```sh
# Set up the venv (one-time)
cd packages/ca65-ls
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"

# Run all tests (85 tests)
.venv/bin/python -m pytest -q

# Run a single test file or test
.venv/bin/python -m pytest tests/test_dbg_oracle.py -v
.venv/bin/python -m pytest tests/test_workspace_index.py::test_indexes_synthetic_corpus -v

# Regenerate test fixtures (.dbg/.lbl/.map) from the CA65 source corpus
bash tools/regen_fixtures.sh

# Run the LSP standalone for ad-hoc smoke testing
.venv/bin/python -m ca65_ls.server --stdio
# Or: .venv/bin/ca65-ls --stdio

# Dump a .dbg as JSON (debug tool)
.venv/bin/python -m ca65_ls.index.dbg_oracle path/to/file.dbg [--name SYMBOL]
```

For the Serena fork's integration tests (M3 onwards):

```sh
cd ~/Documents/serena
.venv/bin/python -m pytest test/solidlsp/ca65/test_ca65_basic.py -v
```

The fork's venv has both Serena and `ca65-ls` installed editable, so changes in this repo's `packages/ca65-ls/` take effect immediately in Serena's tests.

## Scripts for the user

- `scripts/install_local_serena.sh` — swaps Claude Code's MCP config to use the fork + ca65-ls (with backup + revert instructions). User runs this once, then restarts Claude Code.
- `scripts/m4_smoke_test.py [PROJECT_PATH]` — exercises the LSP against any CA65 project (default: c64-https) and prints a Markdown-friendly report. Auto-bootstraps into the Serena fork's venv. Use for collecting M4 bug reports.
- `scripts/symbolic_usage_report.py [--since 24h] [--projects FILE]` — measures how often real CA65 work uses the symbolic tools versus raw reads/edits. Reads Claude Code's own transcripts under `~/.claude/projects/*/*.jsonl`, which record *every* tool call (native `Read`/`Grep`/`Edit` included) with a timestamp and cwd. **Serena's web dashboard cannot answer this**: `/get_tool_stats` counts only Serena MCP calls and resets whenever the MCP process restarts, so it under-reports by construction. Windows are arbitrary and the transcripts are durable, so the script also reconstructs past periods retroactively.

### The usage monitor (installed 2026-08-28)

A LaunchAgent `com.jc000.ca65-symbolic-usage` snapshots a rolling 24h window hourly into `~/.serena/ca65-metrics/snapshots.jsonl`, then writes `final_report.md` and unloads itself once past `~/.serena/ca65-metrics/deadline`. `baseline_prefix.json` in that directory holds the pre-fix measurement to compare against.

Everything it reads is staged under `~/.serena/ca65-metrics/` — a copy of the report script plus `projects.json`, the baked list of in-scope projects. **A launchd job is denied `~/Documents` by TCC**, so it cannot read the repo or the rig config directly; that is why `--projects` exists. If you move the collector back into the repo it will fail with `Operation not permitted`. Remove it with:

```sh
launchctl bootout gui/$(id -u)/com.jc000.ca65-symbolic-usage
rm -rf ~/.serena/ca65-metrics ~/Library/LaunchAgents/com.jc000.ca65-symbolic-usage.plist
```

## Milestone status

**All five milestones done. Upstream PR [oraios/serena#1504](https://github.com/oraios/serena/pull/1504) was REJECTED on 2026-05-26** — closed by maintainer without code review, citing niche audience, the unverified `tree-sitter-ca65` dependency (supply-chain risk), and maintenance overhead. A scope/policy decision, not a quality one. **The fork is now the long-term home**; ongoing work is periodic rebases of `feature/ca65-language-server` onto upstream main, and `scripts/install_local_serena.sh` is the permanent install path.

- ✅ M1 — `.dbg` oracle (`ca65_ls/index/dbg_oracle.py`)
- ✅ M2 — MVP LSP daemon: buffer + workspace index + pygls server
- ✅ M3 — Serena fork wired up; 5/5 e2e tests pass through `request_document_symbols`/`request_definition`/`request_references`
- ✅ M4 — c64-https validated end-to-end; hover + rename + scope-aware refs + cache auto-invalidation all landed. Cold reindex perf 13.4s → 1.67s (8×, under budget).
- ✅ M5 — PR opened CI-green (tests on all 3 OSes, mypy, format/lint, CodeQL); closed unmerged 2026-05-26 (see above).

**PyPI blocker resolved (2026-07-20):** the `tree-sitter-ca65` grammar is vendored into `packages/ca65-ls/vendor/tree-sitter-ca65/` and compiled as the abi3 extension `ca65_ls._grammar._binding` (setuptools backend; provenance/update procedure in the vendor NOTICE.md). No git-URL deps remain; first PyPI release just needs the `RELEASING.md` tag procedure. Upstream ask [pogyomo/tree-sitter-ca65#1](https://github.com/pogyomo/tree-sitter-ca65/issues/1) stays open but is no longer load-bearing.

## Gotchas worth knowing

1. **`a`, `x`, `y`, `s` are reserved** in CA65 (6502 register names) — cannot be used as `.struct` field names; ca65 reports a misleading "Invalid storage allocator in struct/union" error. Cost ~10 min of debugging during M1.
2. **`.export name := $1234` is recorded as `type=lab` in `.dbg`**, not `type=equ`. The semantic difference (label vs equate) is the absence/presence of `seg=N` field. `dbg_oracle.SymbolKind` reflects the raw cc65 type.
3. **Library archives don't contribute `.dbg` sym records.** KERNAL routines etc. have resolved addresses in `.lbl` and `.map` but no source-line debug info. Falls outside the LSP's source-navigation scope.
4. **pygls 2.x imports moved** to `pygls.lsp.server.LanguageServer` (not `pygls.server`).
5. **Stale `.pyc` after parallel-agent edits** can produce confusing `AttributeError` failures that vanish on `find . -name __pycache__ -exec rm -rf {} +`. Worth trying before deeper debugging.
6. **Serena's root `.gitignore` excludes `build/`** — the fork's committed CA65 fixture `.dbg/.lbl/.map` files live in `test/resources/repos/ca65/test_repo/build/` and need `git add -f`. The per-fixture `.gitignore` keeps `.o`/`.prg` (regenerable) out.
7. **The CA65 tools silently don't run for a project unless its `.serena/project.yml` lists `ca65` under `language_servers:`** (upstream v1.7.0 renamed the key from `languages:`; old configs still auto-migrate, and the rig's files are currently a mix of both spellings) — activation succeeds and symbolic tools respond, just without the CA65 backend. Those `project.yml` files are rig-managed symlinks; edit the targets under `~/Documents/new-computer-setup/claude-code-rig/state/serena-config/<project>/`. Serena caches project configs at MCP startup, so config edits need a Claude Code restart — `activate_project` and `restart_language_server` won't reload them. (Audited and fixed across all c64-* projects 2026-08-21; re-audited 2026-08-28 and no gaps remain. `c64-e2ee-chat` and `c64-test-harness` contain no assembly at all; `c64-sid-instruments` has 12 `.asm` files but they are **ACME** syntax (`!byte`/`!word`), auto-generated SID data tables — its own tooling calls them "ACME-includable" — so it must NOT get a `ca65` entry, or tree-sitter-ca65 would parse a foreign dialect into garbage symbols. **A `.s`/`.asm`/`.inc` extension does not imply CA65** — check for `.proc`/`.segment`/`.byte` before adding a project to the corpus.)
8. **A `.export foo` declarator is suppressed when `foo` is also defined in the same file.** `SymbolKind.EXPORT` is the declarator, not the target, so emitting both made every exported routine appear twice — once as a one-token EXPORT, once as the real PROC/LABEL/CONSTANT. That was 24.7% of all symbols on c64-https. A re-export with no in-file definition is still emitted, since there the declarator is all the file says about the name. See `_suppress_redundant_exports` in `ca65_ls/buffer/document.py`.
9. **The workspace cache is keyed only on `(path, mtime_ns, size)`** — it does *not* notice that the indexer's own code changed, so after any parser/symbol-shape edit it will happily keep serving stale symbols from `<project>/.ca65-ls/cache/`. Bump `CACHE_FORMAT_VERSION` in `ca65_ls/index/workspace.py` (and add a note to the version log beneath it) whenever the emitted symbols change, or every one of the eight c64-* projects will silently report pre-change results.

## Conventions

- The fork is on branch `feature/ca65-language-server`, kept rebased onto `oraios/serena` main (force-push with lease after each rebase; last rebase 2026-08-28, commits `ff36caff`+`4004af10`+`ed049eb0` atop `7fcbca7e` — the third commit adds assembly extensions to the hooks' `_CODE_FILE_EXTENSIONS`, and the machine's global Claude Code hooks in `~/.claude/settings.json` run `serena-hooks` from this fork's `.venv`, so keep that venv intact). **Don't `git add -A` inside `~/Documents/serena/`** — claude-code-rig drifts `.serena/memories/*` to symlinks and `.gitignore`, which would silently land in a fork commit. Stage explicit paths. The rig re-drifts these files *between tool calls*, so the drift paths (`.gitignore`, `.serena/project.yml`, and every tracked `.serena/memories/*.md`) carry local `git update-index --skip-worktree` flags. **Derive that path list fresh from `git ls-files .serena/memories/` after each rebase** — upstream adds and removes its own memory files, so a hardcoded list fails with `fatal: Unable to mark file` (it was 11 paths before v1.7.0, 8 after) — fork `git status` looks clean even while the rig symlinks are live; unset with `--no-skip-worktree` if a path must track normally (e.g. before a future rebase touching them).
- Fork validation commands (upstream replaced mypy with **ty**): `uv run poe lint`, `uv run poe type-check`, `uv run pytest test/solidlsp/ca65/test_ca65_basic.py -vv`. **`uv sync` prunes the editable ca65-ls install** — re-run `uv pip install -e ~/Documents/ca65-asm-serena-lsp/packages/ca65-ls` afterwards, or pass `uv run --no-sync` to avoid the prune in the first place.
- Commits include `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` per the user's git workflow.
- Test fixtures in `packages/ca65-ls/tests/fixtures/test_repo/` mirror `test/resources/repos/ca65/test_repo/` in the Serena fork. The source files (`src/*.s`, `inc/*.inc`, `cfg/*.cfg`) are authored here; the build artifacts (`build/test_repo.dbg|lbl|map`) are committed snapshots regenerated by `tools/regen_fixtures.sh`.
- Eight `c64-*` sibling projects under `~/Documents/` are the realism corpus once v1 stabilizes on `c64-https`. Each is a regression test for the indexer.
