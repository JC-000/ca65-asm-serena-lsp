# Retirement: why ca65-ls was not a value add

**Retired 2026-09-22.** The language server works. Agents doing real CA65 work mostly didn't use it,
and when a hook forced the issue they went back to `grep`. The upkeep outweighed what it delivered,
so the project is archived. This page records the evidence, so the decision can be revisited if
anything changes.

## What was built, and whether it worked

Built from May to September 2026: a pygls language server driven by a vendored tree-sitter grammar,
optional `.dbg` enrichment, and an entry-point adapter that plugs into stock Serena. It passed its
own bar:

- Symbols, definitions, scope-aware references, rename, hover and workspace search, all validated
  on a 247-file C64 project and a contract suite over ten real `c64-*` codebases.
- Cold reindex 1.3–1.7s; `.dbg` addresses surfaced in symbol output (`$37E4 in CRYPTO_AUX_CODE2`).
- A final check on 2026-09-22 against c64-https returned correct cross-file references.

The server was never the problem. The problem was getting agents to use it.

## Evidence: adoption

The measure was the share of assembly-navigation calls in Claude Code transcripts that went to
Serena's symbolic tools rather than shell reads and greps (`scripts/symbolic_usage_report.py`).

| window | symbolic share | notes |
|---|---:|---|
| 2026-08-01 → 08-28 (baseline) | 2.8% | 18 of 651 operations, 39 sessions, main sessions only |
| 2026-09-03 → 09-22, main sessions | 9.2% | 26 of 284 |
| same window, **including subagents** | 11.8% | 149 of 1260, 190 sessions |
| same, **excluding c64-wireguard** | **3.6%** | 29 of 795 — no different from baseline |

- **The one exception was told to.** c64-wireguard accounts for 120 of the 149 symbolic calls. Its
  agent definitions (`.claude/agents/adversarial-review.md`, `red-green.md`) list the Serena tools
  and say to use them. Outside that project, a machine-wide CLAUDE.md instruction to prefer the
  symbolic tools made no measurable difference.
- The report originally skipped `subagents/*.jsonl`, which hold about 4× the main-session volume,
  and the 9.2% figure came from that gap. Fixed in this final change.

## Evidence: the nudge hook

`scripts/ca65_bash_nudge.py` denied the third consecutive shell read or grep of assembly and named
the symbolic tool to use instead. An audit of every deny from 2026-09-03 to 09-11 (no CA65 work
after that):

- **349 denies**, 258 of them in subagents.
- The classifier did its job: replaying each command reproduces the deny (except 6 whose worktrees
  have since been deleted). The triggers were `grep` on assembly (194), `sed -n N,Mp` line ranges
  (95), `git show REV:file.s` (13), `cat`/`diff`/others.
- **About 42% were wrong anyway**, because the command was something the LSP cannot answer:
  greps for prose, `§` citations, directives (`\.align`, `\.import`) and version strings, reads of
  history with `git show`, and `diff`s. About 30% were identifier lookups, the case the hook was
  aimed at. About 28% were line-range reads, a partial fit.
- **It didn't change behaviour.** After a deny, the next call was Bash 79% of the time, 152 of
  them the same or nearly the same command retried. A symbolic call came next about 6% of the time.
  Each deny cost the agent a turn.

The hook was uninstalled on 2026-09-22.

## Why grep was good enough

CA65 names are flat and, in practice, unique across a project: `grep -rn name src/` gets very close
to "find references", and `sed -n` on a line number `grep` just gave is close to "read the routine".
The LSP is clearly better in only a few places:

- cheap local labels (`@loop`) and labels scoped inside a `.proc`
- rename across files, including `.export` declarators
- post-link addresses and segments from `.dbg`

These came up rarely in the audited sessions. Most assembly searches were for text the LSP doesn't
model at all: comments, directives, spec citations, build configuration.

## What it cost

- A Serena fork, rebased repeatedly onto upstream. It shrank from 4 commits to 2 once upstream
  added out-of-tree registration, but it never went to zero.
- Machine-wide fragility. Claude Code's global Serena hooks ran from the fork's editable venv, so a
  bad sync or a branch switch changed every session on the machine. `uv sync` quietly pruned the
  ca65-ls install and its dependencies. After one restart the MCP silently loaded an old build from
  a stale config.
- Restarts after every config or dependency change, and a measurement pipeline that needed its own
  LaunchAgent and TCC workarounds.

## Lessons

- **Measure adoption before building the second half.** A week of transcript counting after the M2
  MVP would have shown the same pattern at a fraction of the cost.
- **Instructions in an agent definition work. Global guidance doesn't.** If this is ever revived,
  scope it to agent definitions for specific jobs (e.g. a rename agent), not to general navigation.
- **Blocking hooks need to know what the agent intended.** Recognising the file type isn't enough:
  a hook that blocks grep has to tell a name lookup from a text search, or it teaches agents to retry.
- **Count subagents.** Most of the tool traffic is in `<session>/subagents/*.jsonl`.
- **Transcripts aren't permanent.** Claude Code deletes them after `cleanupPeriodDays` (default 30),
  so any measurement you can reconstruct later only reaches back about a month.

## Where things are left

- This repository is archived read-only. `ca65-ls` 0.1.0 was never published to PyPI, and the
  `ca65-ls-v0.1.0` tag was never pushed.
- The Serena fork branch `JC-000/serena@feature/ca65-external-adapter` (2 commits) and the old
  `feature/ca65-language-server` branch stay on GitHub for archaeology.
- `docs/research/` (the tree-sitter-ca65 coverage matrix and `.dbg` format spec) and
  `ca65_ls/index/dbg_oracle.py` stand on their own and may be useful to other cc65 tooling.
