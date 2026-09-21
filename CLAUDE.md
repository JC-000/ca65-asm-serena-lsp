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
- **`~/Documents/serena/`** (GitHub: `JC-000/serena`, fork of `oraios/serena`) — since 2026-09-20 this carries **only two small commits** against upstream: `.s/.asm/.inc/.mac` added to the reminder hook's `_CODE_FILE_EXTENSIONS`, and the LSP `detail` field surfaced in `LanguageServerSymbol.to_dict`. Branch `feature/ca65-external-adapter`. Nothing CA65-specific lives in the Serena tree any more.

**The whole CA65 integration now lives in this repo**, in `packages/ca65-ls/ca65_ls/serena_adapter.py`. Upstream grew support for out-of-tree language servers (`LanguageServerRegistry` + `ExternalLanguageServerId`, upstream commits `6000f166`/`283140e7`), so ca65-ls registers itself through a `solidlsp.language_server_registration` entry point declared in its own `pyproject.toml`. Serena discovers it automatically whenever ca65-ls is installed into the same environment; the key `ca65` then works in a project's `language_servers:` list exactly as a built-in would.

That removed the recurring rebase pain: the fork no longer touches `ls_config.py`, `conftest.py` or `project.template.yml`, which were the files that conflicted on every rebase. It also settles the licensing question upstream raised when it split Serena (GPL-3.0-or-later) from SolidLSP (MIT) in `6707cd9b` — the adapter is our own MIT code in our own package.

The previous shape (4 commits, ~731 lines in the fork, including an in-tree `Language.CA65` enum member and `test/solidlsp/ca65/`) is preserved on the old branch `feature/ca65-language-server` if anything needs archaeology.

## Architecture (3 layers, pinned interface)

```
   +-----------------+      LSP/JSON-RPC      +---------------------------+
   |   Serena MCP    |  <------ stdio ------> |   ca65-ls (Python+pygls)  |
   |  (stock upstream|                        |   server.py                |
   |   + entry point)|                        |                            |
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

# Run the unit tests on the synthetic fixture (87 tests)
.venv/bin/python -m pytest -q -m "not corpus"

# Run the corpus contract suite against the real c64-* projects (~1 min; skips absent projects)
.venv/bin/python -m pytest tests/corpus -q

# The whole red/green gate (hook + unit + corpus + fork e2e). Run BEFORE and
# AFTER any change to ca65-ls, the shim, or the nudge hook. See docs/red-green-gate.md.
../../scripts/gate.sh            # or --quick to skip the corpus layers

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
# The end-to-end tests moved OUT of the fork and into this repo when CA65 became an
# external adapter; they run under the fork venv, which is where solidlsp lives.
~/Documents/serena/.venv/bin/python -m pytest \
    packages/ca65-ls/tests/serena -q --rootdir packages/ca65-ls
```

The fork's venv has both Serena and `ca65-ls` installed editable, so changes in this repo's `packages/ca65-ls/` take effect immediately in Serena's tests.

## Scripts for the user

- `scripts/install_local_serena.sh` — swaps Claude Code's MCP config to use the fork + ca65-ls (with backup + revert instructions). User runs this once, then restarts Claude Code.
- `scripts/m4_smoke_test.py [PROJECT_PATH]` — exercises the LSP against any CA65 project (default: c64-https) and prints a Markdown-friendly report. Auto-bootstraps into the Serena fork's venv. Use for collecting M4 bug reports.
- `scripts/symbolic_usage_report.py [--since 24h] [--projects FILE]` — measures how often real CA65 work uses the symbolic tools versus raw reads/edits. Reads Claude Code's own transcripts under `~/.claude/projects/*/*.jsonl`, which record *every* tool call (native `Read`/`Grep`/`Edit` included) with a timestamp and cwd. **Serena's web dashboard cannot answer this**: `/get_tool_stats` counts only Serena MCP calls and resets whenever the MCP process restarts, so it under-reports by construction. Windows are arbitrary and the transcripts are durable, so the script also reconstructs past periods retroactively.

### The Bash nudge hook (installed 2026-09-01)

- `scripts/ca65_bash_nudge.py` — a PreToolUse hook wired into `~/.claude/settings.json` with an empty matcher (it must see both `Bash` calls and `mcp__serena__` symbolic calls). Denies the 3rd consecutive Bash read/grep of an assembly file **inside a project whose `.serena/project.yml` enables ca65**, naming the symbolic tool that would have answered the query. A deny resets the counter so the next retry proceeds; any symbolic call also resets it; at most one nudge per 2 minutes.

Exists because **Serena's own reminder hook is blind to `Bash` on Claude Code**: `hooks.py` `is_read_call`/`is_grep_call` match only the native `Read`/`Grep` tool *names* for `CLAUDE_CODE`, while the `GROK` and `CODEX` branches also classify shell commands. Measured sessions ran 203 Bash calls and a single `Read`, so Serena's counter never advanced. Extending Serena's hook instead would fire machine-wide across its ~40 `_CODE_FILE_EXTENSIONS` and add a 4th fork commit to carry through rebases. Unlike Serena's, this one splits compound commands (`cd src && grep foo bar.s`), which its first-token-only parse misses.

To disable, delete the entry from `~/.claude/settings.json`; per-session counters live in `~/.claude/ca65-bash-nudge/`.

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
8. **A `.export foo` declarator is suppressed as a *symbol* when `foo` is also defined in the same file, but since 2026-09-02 its name token is still a *reference*** (so rename edits the declarator; before that fix a rename left `.export old_name` behind and broke the build). `SymbolKind.EXPORT` is the declarator, not the target, so emitting both made every exported routine appear twice — once as a one-token EXPORT, once as the real PROC/LABEL/CONSTANT. That was 24.7% of all symbols on c64-https. A re-export with no in-file definition is still emitted, since there the declarator is all the file says about the name. See `_suppress_redundant_exports` in `ca65_ls/buffer/document.py`.
9. **The workspace cache is keyed only on `(path, mtime_ns, size)`** (format version 6 since 2026-09-02) — it does *not* notice that the indexer's own code changed, so after any parser/symbol-shape edit it will happily keep serving stale symbols from `<project>/.ca65-ls/cache/`. Bump `CACHE_FORMAT_VERSION` in `ca65_ls/index/workspace.py` (and add a note to the version log beneath it) whenever the emitted symbols change, or every one of the eight c64-* projects will silently report pre-change results.

## Conventions

- The fork is on branch `feature/ca65-external-adapter`, kept rebased onto `oraios/serena` main (force-push with lease after each rebase; last rebuilt 2026-09-20 as `0f227f6a`+`4264b046` atop `c4dc91a7` — **two commits**: assembly extensions in the hooks' `_CODE_FILE_EXTENSIONS`, and the LSP `detail` field in `to_dict`. Both cherry-picked cleanly onto 85 commits of upstream drift, because neither touches the registry code upstream rewrote). The machine's global Claude Code hooks in `~/.claude/settings.json` run `serena-hooks` from this fork's `.venv`, so keep that venv intact — a broken sync there degrades *every* Claude Code session on the machine, not just this project's. The guard list is now **9 paths** (upstream added `.serena/memories/repl.md`). **Don't `git add -A` inside `~/Documents/serena/`** — claude-code-rig drifts `.serena/memories/*` to symlinks and `.gitignore`, which would silently land in a fork commit. Stage explicit paths. The rig re-drifts these files *between tool calls*, so the drift paths (`.gitignore`, `.serena/project.yml`, and every tracked `.serena/memories/*.md`) carry local `git update-index --skip-worktree` flags. **Derive that path list fresh from `git ls-files .serena/memories/` after each rebase** — upstream adds and removes its own memory files, so a hardcoded list fails with `fatal: Unable to mark file` (it was 11 paths before v1.7.0, 8 after) — fork `git status` looks clean even while the rig symlinks are live; unset with `--no-skip-worktree` if a path must track normally (e.g. before a future rebase touching them).
- Fork validation commands (upstream replaced mypy with **ty**): `uv run poe lint`, `uv run poe type-check`. The CA65 end-to-end tests are no longer in the fork; run them from this repo (see "Commands"). **`uv sync` prunes the editable ca65-ls install** — re-run `uv pip install -e ~/Documents/ca65-asm-serena-lsp/packages/ca65-ls` afterwards, or pass `uv run --no-sync` to avoid the prune in the first place. **Do not add `--no-deps` to that reinstall**: `uv sync` also prunes ca65-ls's *dependencies* (it took `tree-sitter` out on 2026-09-20), so a `--no-deps` reinstall leaves `ca65_ls` importable-looking but broken at `import tree_sitter`.
- Commits include `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` per the user's git workflow.
- Test fixtures in `packages/ca65-ls/tests/fixtures/test_repo/` mirror `test/resources/repos/ca65/test_repo/` in the Serena fork. The source files (`src/*.s`, `inc/*.inc`, `cfg/*.cfg`) are authored here; the build artifacts (`build/test_repo.dbg|lbl|map`) are committed snapshots regenerated by `tools/regen_fixtures.sh`.
- Eight `c64-*` sibling projects under `~/Documents/` are the realism corpus once v1 stabilizes on `c64-https`. Each is a regression test for the indexer.

10. **Inside a git checkout the indexer enumerates files with `git ls-files -co --exclude-standard`**, not a directory walk (since 2026-09-02). That is what excludes `.claude/worktrees/agent-*/` checkouts (full repo copies left by worktree-isolated agents; on c64-https they were 1748 of 1995 indexed files and gave every routine 7–34 definitions), honours global excludes, and skips nested checkouts. The walk fallback (no git) ignores `.claude/` and `.serena/` by default. A worktree checkout has no `.serena/` of its own, so a worktree agent must activate the worktree path as its project to get a single-copy index; memories are unaffected either way (they are a rig symlink per project).
11. **Three parser fixes rewrite the parse input before tree-sitter sees it** (byte-length-preserving): leading `::` blanked, `a:`/`z:`/`f:` address-size prefixes blanked in operand position, and a colon inserted after column-0 labels in `.feature labels_without_colons` files. Any new rewrite must keep byte lengths, or every column after it is wrong. ACME-dialect files (`!zone`, `!byte`) are sniffed and emit nothing.
12. **The rebase conflicts are now down to zero.** Moving CA65 out of the Serena tree removed the last one: `src/serena/resources/project.template.yml` used to conflict every rebase because its language grid is generated and the fork inserted a `ca65` row. The fork no longer touches that file, so `scripts/print_language_list.py` is not needed any more. The CHANGELOG conflict went earlier; don't add an entry back. The two remaining commits (`hooks.py`, `symbol.py`) are pure additions to files upstream rarely edits, and cherry-picked cleanly across 85 commits of drift.
13. **`scripts/gate.sh` is the pre-change check** for anything touching ca65-ls, the adapter, or the hook: hook suite, unit, corpus contract, through-Serena, entry-point discovery. Red tests are `xfail(strict=True, raises=AssertionError)`; a fix makes them XPASS and fails the gate until the marker is removed. See `docs/red-green-gate.md`.
14. **`uv pip check` cannot tell you the fork venv is broken.** It validates installed packages against *each other*, not against the project's `pyproject.toml`. After switching the fork to 85-commits-newer upstream it reported "All installed packages are compatible" while `mcp` was pinned at `1.28.1` against a required `mcp==2.2.0` — Serena's hooks still worked (they don't import the MCP layer) but `from serena.agent import SerenaAgent` raised `ModuleNotFoundError: No module named 'mcp.server.mcpserver'`, i.e. the MCP server would have died on the next Claude Code restart with no obvious link to the rebase. To actually check, import the thing: `.venv/bin/python -c "from serena.agent import SerenaAgent"`, and compare `grep '"mcp' pyproject.toml` against `importlib.metadata.version("mcp")`.
15. **Switching branches in `~/Documents/serena` silently changes machine-wide behavior**, because the venv installs Serena *editable*: the global `serena-hooks` in `~/.claude/settings.json` immediately starts running whatever source the checkout is on. Probe all four configured subcommands (`remind`, `auto-approve`, `activate`, `cleanup`) after any branch change or sync; each should exit 0.
