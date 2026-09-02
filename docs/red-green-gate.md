# The red/green gate

`scripts/gate.sh` is the pre-change check for anything that touches other
projects: the `ca65-ls` parser and index, the Serena shim in the fork, and the
global Bash nudge hook. Run it before you start and again before you commit.

```sh
scripts/gate.sh          # every layer, ~1.5 min (corpus runs dominate)
scripts/gate.sh --quick  # hook + unit + fork e2e only, ~10 s
```

## The convention

Two colours of test, one rule.

- **Green** tests pin behaviour we rely on. A failure is a regression.
- **Red** tests are marked `xfail(strict=True)` and describe behaviour we have
  decided the tool *should* have but does not yet. Each docstring starts with
  `RED`, the date it was found, and the evidence. They fail today by design.

The rule: when a fix lands, the red test XPASSes and pytest reports that as a
**failure**. Remove the marker and the `RED` note in the same commit as the
fix. The suite therefore always records exactly what is still owed, and a fix
can never land unnoticed.

Red tests over the corpus assert across *all* present projects at once (the
`corpus` fixture) rather than per project, because a defect that only shows in
some projects would otherwise XPASS on the clean ones.

## Layers

| layer | where | runs with | what it proves |
|---|---|---|---|
| hook suite | `tests/hook/` | repo venv | `scripts/ca65_bash_nudge.py` counts, denies, resets and stays silent exactly as documented; malformed input never blocks a call |
| unit tests | `packages/ca65-ls/tests/` | repo venv | parser, index, server handlers on the synthetic fixture |
| corpus contract | `packages/ca65-ls/tests/corpus/` | repo venv | invariants on the real `c64-*` projects (below) |
| through-Serena | `packages/ca65-ls/tests/serena/` | fork venv | ca65-ls started through SolidLSP exactly as Serena does: parity with the direct handlers, lifecycle, staleness |
| fork e2e | `~/Documents/serena/test/solidlsp/ca65/` | fork venv | the shim boots and answers symbols/definition/references |

### Corpus contract

The corpus is listed in `packages/ca65-ls/tests/corpus/corpus.json`; projects
that are not present are skipped, so CI without them passes. Only projects
whose `.serena/project.yml` enables `ca65` belong there. The index is built
with `cache=False`, so the suite never writes into a project and never reads a
stale cache.

Expectations are **derived from the source text**, never hand-written, so they
cannot rot as the projects evolve. `ground_truth.py` is a deliberately naive
scanner that only claims the unambiguous cases (`.proc NAME` at file level,
`jsr NAME` outside macro bodies) and knows how to recognise ACME-dialect files
so it does not hold the CA65 parser to a foreign syntax.

Invariants checked per project:

- the index builds, matches the file walk, is deterministic, and stays within a
  time budget of 2 s + 10 ms per file
- every symbol's `selection_range` spells its own name in the source
- ranges are well-formed and document-symbol children lie inside their parent
- no record is emitted twice; `.export` declarators are suppressed when the
  name is defined in the same file (the 2026-08-28 fix)
- every `.proc`/`.scope`/`.macro`/`.struct`/`.enum` declaration is indexed
- a deterministic sample of cross-file `jsr` sites resolves to its definition,
  and the definition's references include the call site, with no leakage to
  similarly named symbols
- workspace search finds every proc
- nothing under a tool directory or the project's own `.gitignore` is indexed

## Defects found by the suite (2026-09-02)

All are recorded as red tests; none is fixed yet.

1. **Worktree copies are indexed.** `.claude/worktrees/agent-*/` holds complete
   copies of a repo left by worktree-isolated agents. `.gitignore` excludes
   them with `.claude/*`, but `_is_ignored` in `ca65_ls/index/workspace.py`
   asks pathspec about the *file* path only, and `.claude/*` does not match a
   file two levels down (verified with pathspec 1.1.1). On c64-https 1748 of
   1995 indexed files are copies and every routine has 7–34 definitions.
   Affected: c64-https (`.gitignore` says `.claude/*`), and c64-mlkem and
   c64-ChaCha20-Poly1305, which do not ignore `.claude` at all, so honouring
   `.gitignore` correctly would not save them. c64-wireguard and
   c64-nist-curves use a directory pattern (`.claude/worktrees/`, `.claude/`),
   which pathspec does match, so they are clean by luck of spelling. Fix: add
   `.claude/` to `_DEFAULT_IGNORES` *and* match ancestor directories.
2. **Every nested symbol reaches the index twice.** `_ingest_file` walks
   `_flatten(buffer_symbols)` over a list that `Document.flat_symbols()` has
   already flattened, so each child (cheap locals under labels, labels under
   procs) is appended twice as a byte-identical record. 197 of 885 records on
   c64-x25519. `on_workspace_symbol` dedupes at the output, which is why it went
   unnoticed. Fix: drop the `_flatten` call.
3. **Label bodies overrun `.endproc`.** A label that is the last one before
   `.endproc` gets a body running to end-of-file instead of stopping at the
   enclosing proc (c64-x25519 `fe25519.s`: `mul38_hi` 1160–2481 inside
   `mul_by_38` 1115–1161). Clip label bodies to the parent range.
4. **ACME files are parsed as CA65.** c64-nist-curves keeps ACME and CA65 twins
   side by side (`mod256.asm` / `mod256.s`). The grammar turns the ACME twin
   into partial garbage. Sniff `!zone`/`!byte`-style directives and skip.
5. **Hook: cross-project sweeps are counted.** A loop that greps sibling repos
   from another project's cwd is told to use `find_referencing_symbols`, which
   only searches the active project (live example 2026-09-01).
6. **Hook: leading shell keywords hide the command.** `do cat`, `then cat`,
   `time grep`, `env X=1 grep`, `nice cat` are never classified because only
   the first token of each segment is examined.

### From the index review (13 findings, 2026-09-02)

Synthetic reproducers in `packages/ca65-ls/tests/test_review_red.py`, corpus
versions in `tests/corpus/test_review_findings.py`. Beyond items 1–4 above:

- **Destructive rename of proc-local labels.** Because the label body overruns
  `.endproc` (item 3), `_range_strictly_inside` fails and references and
  rename go project-wide: c64-mlkem `sponge.s:235` `done:` reports 65
  references and rename edits four unrelated routines. 36 of 107 proc-nested
  labels overflow. `on_definition` has no scope filtering at all.
- **Rename skips the `.export` declarator.** The 2026-08-28 suppression fix
  also dropped it from references, so a rename leaves `.export old_name` and
  the build breaks. 3885 declarators across the corpus.
- **Macro arguments are invisible**: `stax tcp_callback` creates no
  reference. 1390 of 1390 such sites; ip65 code is almost entirely macros.
- **`.feature labels_without_colons` files collapse** into one ERROR node:
  656 labels lost in c64-https' three vt100 drivers.
- **`.if ::NAME` swallows the next constant**, and no `::NAME` reference is
  recorded (111 sites). **`.define` and `.set` never become symbols.**
- **Definition prefers a foreign `.proc` over the same-file label**
  (c64-wireguard `print_string`, 21 names).
- **`a:`/`z:` address-size prefixes** parse as anonymous labels; **macro-body
  and `.local` labels** surface as file-scope symbols.
- **Corpus classification**: Amiga64 is CA65 and is now in `corpus.json`;
  1541ultimate is a mix of GNU as, 64tass and MOS syntax and stays out.

Clean: no crashes or slow files across ~800 real sources, `.dbg` addresses
match the linker map 959/959, CRLF/tabs/65816/spaced segment names parse,
cache invalidation on edit, version bump and deletion behaves.

### From the Serena-layer review (15 findings, 2026-09-02)

Through-Serena tests in `packages/ca65-ls/tests/serena/` (fork venv). New
beyond the index items:

- **Columns are UTF-8 byte offsets.** On a line with non-ASCII text before
  the identifier, rename mangles the line (`'.byte "日本語", >target'` became
  `>targettgtper`). 78 such lines in the corpus. Unit red test.
- **The index never refreshes after edits through Serena.** Only `didSave`
  reindexes, and Serena never sends it; `didChange` and
  `didChangeWatchedFiles` do nothing. After an agent adds or edits a routine,
  definition and references stay stale until the server restarts. Red.
- **Fixed 2 s sleep on the first cross-file request** of every server start:
  the shim inherits SolidLSP's wait although ca65-ls indexes synchronously in
  `initialize`. Red.
- **Symlinked project root** yields `../../..` relative paths and
  `request_full_symbol_tree` raises `ValueError`. Red.
- **Duplicate definition edits corrupt the token through Serena's rename**:
  three identical edits on `@loop` are all applied, giving `@againnn`. Same
  root cause as item 2; covered by the one-edit-per-site unit red.
- **`.dbg` addresses freeze**: the server never reloads the `.dbg`, and
  Serena's cache fingerprint ignores it. Not encoded yet.
- **ca65-ls echoes every JSON-RPC payload to stderr at INFO**, and Serena
  re-logs lines containing `error` (`ip65_error`) as ERROR. Not encoded.
- **Missing or crashing ca65-ls** surfaces as a generic initialize failure
  with the cause on a separate log line; a hang takes 235 s to time out.
  Not encoded (needs a deliberately broken install).
- **`languages:` and `language_servers:` both present**: the old key is
  silently ignored. Upstream Serena behaviour, noted for the rig config.

Design decision, not encoded: `find_symbol` returns `.import` declarators as
Interface hits (one per importing file, 6049 in the corpus). Either filter
IMPORT out of workspace/document symbols or keep them as navigation aids.

### From the hook review (23 findings, 2026-09-02)

Encoded as red tests in `tests/hook/`:

7. **Heredoc bodies are scanned** (`cat > report.md <<EOF` mentioning a `.s`
   file counts). Seen live 2026-09-02T00:12Z: a subagent was denied while
   *writing its audit report*.
8. **Pipe stages count as file reads** (`ca65 src/main.s | head`). Both live
   denies on 2026-09-01 fired because of a trailing `| head`.
9. **Quoted `;` and `|` split commands** (`git commit -m "Trim main.s; cat …"`).
10. **`sed --in-place`, `-Ei`, `-ni` count as reads.**
11. **Suffix matches inside longer names** (`main.s.bak`, `config.inc.php`,
    `/Users/name.s/`).
12. **Multi-line commands are never split on newlines**; only line one is seen.
13. **Directory greps with no literal `.s`** (`grep -rn foo src/`, `rg -t asm`)
    never count, and that is the headline use case.
14. **More wrappers hide the reader**: `while read`, `( … )`, `{ …; }`,
    `LC_ALL=C grep`, `timeout 10 cat`, `\cat`, `$(cat …)`, `cat<file`.
15. **Other readers are unknown**: `xargs cat`, `find -exec cat`, `git grep`,
    `git show HEAD:x.s`, `diff a.s b.s`.
16. **Brace expansion and `.S`** are missed.
17. **No walk-up from a subdirectory cwd** (`cd src` silences the hook).
18. **Valid YAML spellings missed** in `project.yml`: flow style, trailing
    comment, BOM before the key.
19. **A renamed MCP server prefix** (`mcp__serena-local__find_symbol`) does
    not reset the counter.
20. **A future `last_deny` in the state file** keeps the quiet window open
    for the whole session; a bad count with a live `last_seen` raises on
    every call and is swallowed.
21. **Quadratic path regex**: a 100 kB heredoc stalled the hook for 21 s.

Design decisions, not encoded (decide, then add a red or a green test):

- **Quiet window** (F16): after a deny, counting is suspended for two minutes,
  so a retry and the next ten reads are all free. Pinned green as intentional
  for now. Alternative: keep counting during the window, deny again when it
  closes.
- **Subagents share the parent's `session_id`** (F15): parallel agents share
  one counter and race on one file. The payload carries no agent id.
- **Debatable readers** (F07, part of F11): `grep -q … && make`, `grep -c`,
  `head -c 0`, `python -c open().read()`, `eval`, `od`.
- **No cleanup of state files** (F20) and **no coordination with Serena's own
  remind hook** (F23): never a double deny on one call, but back-to-back
  denies across a mixed burst.
