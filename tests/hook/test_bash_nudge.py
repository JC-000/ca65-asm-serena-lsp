"""Red/green suite for scripts/ca65_bash_nudge.py.

GREEN tests pin the behaviour the hook is documented to have. They must keep
passing across any change to the hook.

RED tests (``xfail(strict=True)``) encode behaviour we have decided the hook
*should* have but does not yet. They fail today; once the hook is fixed they
XPASS, pytest reports that as a failure, and the test is then flipped to green
by removing the marker. That way the suite itself records what is still owed.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import CA65_YML_NEW_KEY, HOOK, Hook, make_project

red = pytest.mark.xfail(strict=True, raises=AssertionError)

READ = "sed -n '10,40p' src/main.s"
GREP = "grep -n 'jsr' src/main.s"


# ============================================================ GREEN: counting


def test_two_reads_pass_third_is_denied(hook: Hook):
    first, second, third = hook.burst(READ, 3)
    assert not first.denied and not second.denied
    assert third.denied
    assert "find_symbol" in third.reason and "include_body=True" in third.reason


def test_third_grep_names_find_referencing_symbols(hook: Hook):
    third = hook.burst(GREP, 3)[-1]
    assert third.denied
    assert "find_referencing_symbols" in third.reason


def test_reads_and_greps_share_one_counter(hook: Hook):
    hook.bash(READ)
    hook.bash(GREP)
    assert hook.bash(READ).denied


def test_deny_resets_counter_so_retry_proceeds(hook: Hook):
    """The deny is a speed bump, not a wall: the very next retry goes through.

    This is intentional (an agent must never be locked out of a file), so it
    is pinned as green. The cost is that a verbatim retry is all it takes to
    ignore the nudge; see the 2026-09-01 handoff memory for the live example.
    """
    assert hook.burst(READ, 3)[-1].denied
    assert not hook.bash(READ).denied
    # Inside the two-minute quiet window the hook does not count at all, so a
    # retry leaves the counter at zero rather than starting a new burst.
    assert hook.state()["count"] == 0


def test_symbolic_call_resets_counter(hook: Hook):
    hook.bash(READ)
    hook.bash(READ)
    hook.symbolic("find_symbol")
    assert not hook.bash(READ).denied
    assert not hook.bash(READ).denied
    assert hook.bash(READ).denied


@pytest.mark.parametrize(
    "tool",
    [
        "find_symbol",
        "get_symbols_overview",
        "find_referencing_symbols",
        "replace_symbol_body",
        "rename_symbol",
    ],
)
def test_every_symbolic_tool_resets(hook: Hook, tool: str):
    hook.burst(READ, 2)
    hook.symbolic(tool)
    assert hook.state()["count"] == 0


def test_non_symbolic_serena_tool_does_not_reset(hook: Hook):
    hook.burst(READ, 2)
    hook.call("mcp__serena__read_file", {"relative_path": "src/main.s"})
    assert hook.bash(READ).denied


def test_other_tools_are_ignored(hook: Hook):
    for _ in range(5):
        d = hook.call("Read", {"file_path": str(hook.cwd / "src/main.s")})
        assert not d.denied and d.returncode == 0
    assert hook.state().get("count", 0) == 0


# ============================================================ GREEN: timing


def test_only_one_deny_per_two_minutes(hook: Hook):
    assert hook.burst(READ, 3)[-1].denied
    # Within the quiet interval a fresh burst is allowed through silently.
    assert not any(d.denied for d in hook.burst(READ, 4))


def test_deny_interval_expires(hook: Hook):
    assert hook.burst(READ, 3)[-1].denied
    hook.age_last_deny(121)
    assert hook.burst(READ, 3)[-1].denied


def test_long_gap_starts_a_new_burst(hook: Hook):
    hook.burst(READ, 2)
    hook.age_last_seen(601)
    assert not hook.bash(READ).denied
    assert hook.state()["count"] == 1


# ============================================================ GREEN: scoping


def test_silent_in_project_without_ca65(home, python_project):
    h = Hook(home, python_project)
    assert not any(d.denied for d in h.burst(READ, 6))
    assert not h.state_path.exists()


def test_silent_in_project_without_serena_config(home, bare_project):
    h = Hook(home, bare_project)
    assert not any(d.denied for d in h.burst(READ, 6))


def test_language_servers_key_spelling_is_recognised(home, tmp_path):
    project = make_project(tmp_path, "newkey", CA65_YML_NEW_KEY)
    h = Hook(home, project)
    assert h.burst(READ, 3)[-1].denied


def test_missing_cwd_is_silent(hook: Hook):
    for _ in range(4):
        d = hook.call(
            "Bash",
            {"command": READ},
            raw=json.dumps(
                {
                    "session_id": hook.session_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": READ},
                }
            ),
        )
        assert not d.denied


# ============================================================ GREEN: classifier


@pytest.mark.parametrize(
    "command",
    [
        "cat src/main.s",
        "head -50 src/lib.inc",
        "tail -n 20 src/main.s",
        "awk 'NR>10' src/main.s",
        "cd src && grep foo main.s",
        "ls src; sed -n '1,5p' src/main.s",
        "grep -rn 'jsr' --include='*.s' .",
        "rg 'start' src/main.s",
        "cat ~/c64-thing/src/main.s",
        "grep -n foo src/macros.mac",
        "cat src/thing.asm",
    ],
)
def test_counts_reads_and_greps_of_assembly(hook: Hook, command: str):
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/foo/bar/' src/main.s",
        "sed -i '' 's/foo/bar/' src/main.s",
        "ca65 -g src/main.s -o build/main.o",
        "cl65 -t c64 src/main.s",
        "make build/main.o",
        "git add src/main.s",
        "ls -la src/main.s",
        "cat README.md",
        "grep foo src/main.c",
        "cat foo.style",
        "grep pattern notes.txt",
        "cat package.json",
    ],
)
def test_ignores_non_reads_and_non_assembly(hook: Hook, command: str):
    assert not any(d.denied for d in hook.burst(command, 4)), command
    assert hook.state().get("count", 0) == 0, command


# ============================================================ GREEN: robustness


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "{}",
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": ["cat", "main.s"]}}),
        json.dumps({"tool_name": None, "tool_input": None, "session_id": None, "cwd": None}),
    ],
)
def test_malformed_payloads_never_block(hook: Hook, raw: str):
    d = hook.call("Bash", raw=raw)
    assert d.returncode == 0 and not d.denied


def test_corrupt_state_file_is_tolerated(hook: Hook):
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.write_text("{not json")
    assert not any(d.denied for d in hook.burst(READ, 2))
    assert hook.bash(READ).denied


def test_deny_output_matches_claude_code_contract(hook: Hook):
    out = json.loads(hook.burst(READ, 3)[-1].stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert isinstance(hso["permissionDecisionReason"], str) and hso["permissionDecisionReason"]


def test_hook_is_executable_and_has_shebang():
    assert HOOK.stat().st_mode & 0o111, "settings.json runs the script directly"
    assert HOOK.read_text().startswith("#!/usr/bin/env python3")


def test_hook_exits_zero_on_internal_error(hook: Hook, monkeypatch):
    # Unwritable state dir must not turn into a blocked tool call.
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.mkdir()  # a directory where the file should be
    d = hook.bash(READ)
    assert d.returncode == 0 and not d.denied


# ============================================================ RED: owed fixes


@red
def test_sweep_over_other_projects_is_not_counted(home, ca65_project, other_ca65_project):
    """Live misfire, 2026-09-01: from c64-lib-contract the agent grepped
    sibling repos in one loop and was told to use find_referencing_symbols,
    which only searches the active project. A command whose assembly paths all
    lie outside the cwd project should not count towards the nudge.
    """
    h = Hook(home, ca65_project)
    # Same shape as the live command: the grep sits in its own `;` segment.
    cmd = f"cd {other_ca65_project.parent} && for r in c64-other; do echo \"== $r\"; grep -rnE 'start' $r/src/*.s; done"
    assert not any(d.denied for d in h.burst(cmd, 4))


@red
@pytest.mark.parametrize(
    "command",
    [
        "for f in src/*.s; do cat $f; done",
        "if true; then cat src/main.s; fi",
        "time grep -n jsr src/main.s",
        "env LC_ALL=C grep -n jsr src/main.s",
        "nice cat src/main.s",
        "command cat src/main.s",
    ],
)
def test_shell_keywords_and_wrappers_before_the_command(hook: Hook, command: str):
    """Found while writing the sweep test: the classifier reads only the first
    token of each segment, so `do cat`, `then cat`, `time grep`, `env … grep`
    are invisible. The live sweep only registered because its grep followed a
    `;`. Skip leading keywords/wrappers before classifying.
    """
    assert hook.burst(command, 3)[-1].denied, command


@red
def test_absolute_path_into_other_project_is_not_counted(home, ca65_project, other_ca65_project):
    h = Hook(home, ca65_project)
    cmd = f"sed -n '1,20p' {other_ca65_project}/src/main.s"
    assert not any(d.denied for d in h.burst(cmd, 4))


def test_absolute_path_into_own_project_is_counted(hook: Hook):
    cmd = f"sed -n '1,20p' {hook.cwd}/src/main.s"
    assert hook.burst(cmd, 3)[-1].denied


# ============================================================ RED: adversarial review 2026-09-02
#
# Findings from the hook review (scratch findings.md F01-F23). Each red test
# below encodes an expectation we have decided on; findings that are design
# decisions (F07 debatable readers, F15 shared subagent counters, F16 the
# quiet window, F20 cleanup, F23 coordination with Serena's hook) are NOT
# encoded here and are tracked in docs/red-green-gate.md instead.


@red
@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.md <<'EOF'\n# Plan\nRefactor src/main.s tomorrow.\nEOF",
        "cat > report.md <<EOF\nAudit of src/lib.inc\nEOF",
    ],
)
def test_heredoc_bodies_are_not_reads(hook: Hook, command: str):
    """F01, seen live 2026-09-02T00:12Z: a subagent writing its audit report
    with `cat > file <<EOF` was denied because the report mentioned a .s file.
    Nothing is read; heredoc bodies must be ignored."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@red
@pytest.mark.parametrize(
    "command",
    [
        "ca65 -t c64 src/main.s -o build/main.o 2>&1 | head -30",
        "make 2>&1 | grep -n main.s",
        "git log --oneline -- src/main.s | head",
        "ls -la src/*.s | cat",
        "git diff --stat src/main.s | tail -3",
    ],
)
def test_pipe_stage_readers_are_not_file_reads(hook: Hook, command: str):
    """F02: a `| head`/`| grep`/`| cat` stage consumes the pipe, not the
    assembly file named earlier in the pipeline. The two live denies on
    2026-09-01 fired because of trailing `| head` stages."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@red
@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Trim main.s; cat cleanup"',
        "grep -n 'foo|cat main.s' README.md",
        "echo 'see main.s; cat it later'",
    ],
)
def test_quoted_separators_do_not_split(hook: Hook, command: str):
    """F04: `;` and `|` inside quotes are data, not command separators."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@red
@pytest.mark.parametrize(
    "command",
    [
        "sed --in-place 's/a/b/' src/main.s",
        "sed -Ei 's/a/b/' src/main.s",
        "sed -ni '/foo/p' src/main.s",
    ],
)
def test_all_in_place_sed_spellings_are_edits(hook: Hook, command: str):
    """F05: only a bare `-i` is recognised as an edit today."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@red
@pytest.mark.parametrize(
    "command",
    [
        "cat src/main.s.bak",
        "cat src/x.inc.orig",
        "cat config.inc.php",
        "tail build.s.log",
        "cat /Users/name.s/notes.txt",
        "head docs/main.s.md",
    ],
)
def test_suffix_must_end_the_filename(hook: Hook, command: str):
    """F06: `.s`/`.inc` inside a longer name is not an assembly file."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@red
@pytest.mark.parametrize(
    "command",
    [
        "cd src\ncat main.s",
        "ls\nsed -n '1,20p' src/main.s",
    ],
)
def test_multi_line_commands_are_split_on_newlines(hook: Hook, command: str):
    """F08: multi-line Bash is the norm and only the first line is seen."""
    assert hook.burst(command, 3)[-1].denied, command


@red
@pytest.mark.parametrize(
    "command",
    [
        "grep -rn 'jsr foo' src/",
        "rg 'jsr foo' src",
        "rg -t asm 'jsr foo'",
        "grep -rn foo .",
    ],
)
def test_directory_greps_in_a_ca65_project_count(hook: Hook, command: str):
    """F09: the headline use case names no literal .s file, so it is never
    counted. A recursive grep over a directory of this project that holds
    assembly files is source navigation and should count."""
    assert hook.burst(command, 3)[-1].denied, command


@red
@pytest.mark.parametrize(
    "command",
    [
        "while read f; do cat $f; done < list.txt",
        "( cat src/main.s )",
        "{ cat src/main.s; }",
        "LC_ALL=C grep -n foo src/main.s",
        "timeout 10 cat src/main.s",
        "\\cat src/main.s",
        "x=$(cat src/main.s)",
        "cat<src/main.s",
    ],
)
def test_more_wrappers_and_shell_forms(hook: Hook, command: str):
    """F10, extending test_shell_keywords_and_wrappers_before_the_command."""
    assert hook.burst(command, 3)[-1].denied, command


@red
@pytest.mark.parametrize(
    "command",
    [
        "find src -name '*.s' | xargs cat",
        "find src -name '*.s' -exec cat {} +",
        "git grep -n foo -- '*.s'",
        "git show HEAD:src/main.s",
        "diff src/main.s src/old.s",
    ],
)
def test_other_readers_count(hook: Hook, command: str):
    """F11 (the unambiguous subset; `python -c`, `eval`, `od` are left out)."""
    assert hook.burst(command, 3)[-1].denied, command


@red
@pytest.mark.parametrize("command", ["cat src/{main,util}.s", "cat src/MAIN.S"])
def test_brace_expansion_and_uppercase_suffix(hook: Hook, command: str):
    """F12: ca65-ls itself lowercases suffixes, so `.S` is a source file."""
    assert hook.burst(command, 3)[-1].denied, command


@red
def test_subdirectory_cwd_walks_up_to_the_project(home, ca65_project):
    """F13: Claude Code reports the shell cwd; after `cd src` the hook no
    longer finds `.serena/project.yml` and goes silent."""
    h = Hook(home, ca65_project / "src")
    assert h.burst("cat main.s", 3)[-1].denied


@red
@pytest.mark.parametrize(
    "yml",
    [
        "project_name: p\nlanguages: [python, ca65]\n",
        "project_name: p\nlanguages:\n- python\n- ca65  # 6502 assembly\n",
        "\ufefflanguages:\n- ca65\nproject_name: p\n",
    ],
    ids=["flow-style", "trailing-comment", "bom"],
)
def test_valid_yaml_spellings_are_recognised(home, tmp_path, yml):
    """F14."""
    project = make_project(tmp_path, "yamlproj", yml)
    assert Hook(home, project).burst(READ, 3)[-1].denied


@red
def test_renamed_serena_server_prefix_still_resets(hook: Hook):
    """F17: the MCP server can be registered under another name
    (`mcp__serena-local__…`); a symbolic call through it must still reset."""
    hook.burst(READ, 2)
    hook.call("mcp__serena-local__find_symbol", {"name_path_pattern": "x"})
    assert not hook.bash(READ).denied


@red
@pytest.mark.parametrize(
    "state",
    [
        {"last_deny": 4102444800.0},
        {"count": "abc", "last_seen": None},
    ],
    ids=["future-last_deny", "bad-count-in-live-burst"],
)
def test_state_that_survives_load_still_recovers(hook: Hook, state: dict):
    """F18 (the part that reproduces): a `last_deny` in the future keeps the
    quiet window open for the rest of the session; a non-integer count with a
    live `last_seen` raises on every call and is swallowed, so the hook goes
    silent. Both should rewrite the state and carry on. (A bad count with no
    `last_seen` already recovers via the 600 s reset and is pinned green.)"""
    import time

    if state.get("last_seen", 0) is None:
        state["last_seen"] = time.time()
    hook.set_state(**state)
    assert any(d.denied for d in hook.burst(READ, 3)), "hook went silent for the session"


@red
def test_huge_command_is_classified_quickly(hook: Hook):
    """F21: the path regex is quadratic on long runs of word characters; a
    100 kB heredoc stalled the hook for 21 s (and the 600 s hook timeout
    would then allow the call). Must finish in well under a second."""
    import time

    blob = "a" * 30_000
    command = f"cat > out.txt <<'EOF'\n{blob}\nEOF"
    payload = json.dumps(
        {
            "session_id": hook.session_id,
            "cwd": str(hook.cwd),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )
    start = time.perf_counter()
    try:
        subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env={"HOME": str(hook.home), "PATH": "/usr/bin:/bin"},
            timeout=1.0,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("hook took longer than 1 s on a 30 kB command") from None
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize("state", ['{"count": "abc"}', '{"count": null}', '{"count": [1]}'])
def test_non_integer_count_without_last_seen_recovers(hook: Hook, state: str):
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.write_text(state)
    assert hook.burst(READ, 3)[-1].denied


@pytest.mark.parametrize(
    "command", ["sed -i.bak 's/a/b/' src/main.s", "tee notes.md <<'EOF'\nsee src/main.s\nEOF"]
)
def test_already_handled_edit_forms(hook: Hook, command: str):
    assert not any(d.denied for d in hook.burst(command, 4)), command
