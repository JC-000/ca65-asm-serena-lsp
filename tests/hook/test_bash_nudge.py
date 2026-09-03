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
import os
import subprocess
import sys
import time

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
    is pinned as green. Since the 2026-09-02 review (F16) the retry is still
    counted: the quiet window suppresses denies, not counting, so a burst that
    simply carries on is denied again the moment the window closes.
    """
    assert hook.burst(READ, 3)[-1].denied
    assert not hook.bash(READ).denied
    assert hook.state()["count"] == 1


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


def test_renamed_serena_server_prefix_still_resets(hook: Hook):
    """F17: the MCP server can be registered under another name
    (`mcp__serena-local__…`); a symbolic call through it must still reset."""
    hook.burst(READ, 2)
    hook.call("mcp__serena-local__find_symbol", {"name_path_pattern": "x"})
    assert not hook.bash(READ).denied


def test_symbolic_tool_on_another_server_does_not_reset(hook: Hook):
    hook.burst(READ, 2)
    hook.call("mcp__other__find_symbol", {"name_path_pattern": "x"})
    assert hook.bash(READ).denied


def test_other_tools_are_ignored(hook: Hook):
    for _ in range(5):
        d = hook.call("Read", {"file_path": str(hook.cwd / "src/main.s")})
        assert not d.denied and d.returncode == 0
    assert hook.state().get("count", 0) == 0


# ============================================================ GREEN: timing


def test_only_one_deny_per_two_minutes(hook: Hook):
    assert hook.burst(READ, 3)[-1].denied
    # Within the quiet interval a fresh burst is allowed through silently,
    # but it is still counted (F16).
    assert not any(d.denied for d in hook.burst(READ, 4))
    assert hook.state()["count"] == 4


def test_burst_that_continues_past_the_window_is_denied_on_the_next_read(hook: Hook):
    """F16 (decided 2026-09-02): the window suppresses denies, not counting."""
    assert hook.burst(READ, 3)[-1].denied
    assert not any(d.denied for d in hook.burst(READ, 4))
    hook.age_last_deny(121)
    assert hook.bash(READ).denied


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


@pytest.mark.parametrize(
    "yml",
    [
        "project_name: p\nlanguages: [python, ca65]\n",
        "project_name: p\nlanguages:\n- python\n- ca65  # 6502 assembly\n",
        "﻿languages:\n- ca65\nproject_name: p\n",
        "project_name: p\nlanguages:\n  - python\n  - 'ca65'\n",
        "project_name: p\nlanguage_servers: ca65\n",
    ],
    ids=["flow-style", "trailing-comment", "bom", "indented-quoted", "scalar"],
)
def test_valid_yaml_spellings_are_recognised(home, tmp_path, yml):
    """F14."""
    project = make_project(tmp_path, "yamlproj", yml)
    assert Hook(home, project).burst(READ, 3)[-1].denied


@pytest.mark.parametrize(
    "yml",
    [
        "project_name: p\nlanguages:\n- python\n# - ca65\n",
        "project_name: p\nlanguages:\n- python\nignored_paths:\n- ca65\n",
        "project_name: p\nlanguages:\n- python\ninitial_prompt: |\n  languages:\n  - ca65\n",
        "project_name: p\nlanguages_disabled:\n- ca65\nlanguages:\n- python\n",
        # Both keys present: language_servers wins, as in Serena itself.
        "project_name: p\nlanguages:\n- ca65\nlanguage_servers:\n- python\n",
    ],
    ids=["commented-out", "other-key", "in-block-scalar", "prefix-key", "both-keys"],
)
def test_yaml_that_does_not_enable_ca65_is_silent(home, tmp_path, yml):
    project = make_project(tmp_path, "yamlproj", yml)
    assert not any(d.denied for d in Hook(home, project).burst(READ, 4))


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


def test_subdirectory_cwd_walks_up_to_the_project(home, ca65_project):
    """F13: Claude Code reports the shell cwd; after `cd src` the hook must
    still find the project's `.serena/project.yml`."""
    h = Hook(home, ca65_project / "src")
    assert h.burst("cat main.s", 3)[-1].denied


def test_sweep_over_other_projects_is_not_counted(home, ca65_project, other_ca65_project):
    """Live misfire, 2026-09-01: from c64-lib-contract the agent grepped
    sibling repos in one loop and was told to use find_referencing_symbols,
    which only searches the active project. A command whose assembly paths all
    lie outside the cwd project must not count towards the nudge.
    """
    h = Hook(home, ca65_project)
    # Same shape as the live command: the grep sits in its own `;` segment.
    cmd = f"cd {other_ca65_project.parent} && for r in c64-other; do echo \"== $r\"; grep -rnE 'start' $r/src/*.s; done"
    assert not any(d.denied for d in h.burst(cmd, 4))


def test_same_sweep_over_own_project_is_counted(home, ca65_project):
    h = Hook(home, ca65_project)
    cmd = f"cd {ca65_project.parent} && for r in c64-thing; do echo \"== $r\"; grep -rnE 'start' $r/src/*.s; done"
    assert h.burst(cmd, 3)[-1].denied


def test_absolute_path_into_other_project_is_not_counted(home, ca65_project, other_ca65_project):
    h = Hook(home, ca65_project)
    cmd = f"sed -n '1,20p' {other_ca65_project}/src/main.s"
    assert not any(d.denied for d in h.burst(cmd, 4))


def test_absolute_path_into_own_project_is_counted(hook: Hook):
    cmd = f"sed -n '1,20p' {hook.cwd}/src/main.s"
    assert hook.burst(cmd, 3)[-1].denied


@pytest.mark.parametrize(
    "command",
    [
        "cat ../c64-other/src/main.s",
        "cd ../c64-other && cat src/main.s",
        "cd ../c64-other\ngrep -rn foo src/",
        "grep -rn foo ../c64-other/src",
        "cd /tmp && cat main.s",
        "(cd ../c64-other && cat src/main.s)",
        "git show HEAD:../c64-other/src/main.s",
    ],
)
def test_paths_outside_the_project_are_not_counted(home, ca65_project, other_ca65_project, command):
    """F03: Serena's tools only see the active project, so reads elsewhere
    cannot be answered by them and must not be nudged."""
    h = Hook(home, ca65_project)
    assert not any(d.denied for d in h.burst(command, 4)), command


def test_cd_inside_a_subshell_does_not_leak(home, ca65_project, other_ca65_project):
    h = Hook(home, ca65_project)
    assert h.burst("(cd ../c64-other && ls); cat src/main.s", 3)[-1].denied


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


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.md <<'EOF'\n# Plan\nRefactor src/main.s tomorrow.\nEOF",
        "cat > report.md <<EOF\nAudit of src/lib.inc\nEOF",
        "cat > src/new.s <<'EOF'\n  .proc new\n  rts\n  .endproc\nEOF",
        "cat <<-EOF > out.md\n\tsee src/main.s\n\tEOF",
        "awk 'BEGIN{for(i=0;i<256;i++)print \".byte \" i}' > src/table.inc",
    ],
)
def test_heredoc_bodies_and_write_targets_are_not_reads(hook: Hook, command: str):
    """F01, seen live 2026-09-02T00:12Z: a subagent writing its audit report
    with `cat > file <<EOF` was denied because the report mentioned a .s file.
    Nothing is read; heredoc bodies and `>` targets are ignored."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


def test_a_read_after_a_heredoc_is_still_seen(hook: Hook):
    """Stripping the body must not swallow the commands that follow it."""
    cmd = "cat > notes.md <<'EOF'\nplan\nEOF\ncat src/main.s"
    assert hook.burst(cmd, 3)[-1].denied


@pytest.mark.parametrize(
    "command",
    [
        "ca65 -t c64 src/main.s -o build/main.o 2>&1 | head -30",
        "make 2>&1 | grep -n main.s",
        "git log --oneline -- src/main.s | head",
        "ls -la src/*.s | cat",
        "git diff --stat src/main.s | tail -3",
        "wc -l src/*.s | sort -n | tail -3",
        "git status | grep main.s",
    ],
)
def test_pipe_stage_readers_are_not_file_reads(hook: Hook, command: str):
    """F02: a `| head`/`| grep`/`| cat` stage consumes the pipe, not the
    assembly file named earlier in the pipeline. The two live denies on
    2026-09-01 fired because of trailing `| head` stages."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


def test_a_read_that_feeds_a_pipe_is_still_a_read(hook: Hook):
    assert hook.burst("cat src/main.s | grep -n jsr", 3)[-1].denied


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Trim main.s; cat cleanup"',
        "grep -n 'foo|cat main.s' README.md",
        "echo 'see main.s; cat it later'",
        'git commit -m "Fix $(date); cat src/main.s"',
    ],
)
def test_quoted_separators_do_not_split(hook: Hook, command: str):
    """F04: `;` and `|` inside quotes are data, not command separators."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@pytest.mark.parametrize(
    "command",
    [
        "sed --in-place 's/a/b/' src/main.s",
        "sed --in-place=.bak 's/a/b/' src/main.s",
        "sed -Ei 's/a/b/' src/main.s",
        "sed -ni '/foo/p' src/main.s",
        "sed -i -e 's/a/b/' src/main.s",
        "sed -i'' 's/a/b/' src/main.s",
        "sed -i.bak 's/a/b/' src/main.s",
    ],
)
def test_all_in_place_sed_spellings_are_edits(hook: Hook, command: str):
    """F05: every in-place spelling is an edit, not a read."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@pytest.mark.parametrize(
    "command",
    [
        "cat src/main.s.bak",
        "cat src/x.inc.orig",
        "cat config.inc.php",
        "tail build.s.log",
        "cat /Users/name.s/notes.txt",
        "head docs/main.s.md",
        "tail -f logs/build.s.d/output.log",
    ],
)
def test_suffix_must_end_the_filename(hook: Hook, command: str):
    """F06: `.s`/`.inc` inside a longer name is not an assembly file."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@pytest.mark.parametrize(
    "command",
    [
        "cd src\ncat main.s",
        "ls\nsed -n '1,20p' src/main.s",
        "cat README.md\ncat src/main.s\n",
        "cat src/main.s \\\n  src/lib.inc",
        "# look at the entry point\ncat src/main.s",
    ],
)
def test_multi_line_commands_are_split_on_newlines(hook: Hook, command: str):
    """F08: multi-line Bash is the norm; every line is classified."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn 'jsr foo' src/",
        "rg 'jsr foo' src",
        "rg -t asm 'jsr foo'",
        "grep -rn foo .",
        "grep -R foo",
        "rg foo",
        "grep --recursive foo src",
        "rg -g '*.s' foo",
    ],
)
def test_directory_greps_in_a_ca65_project_count(hook: Hook, command: str):
    """F09: the headline use case names no literal .s file. A recursive grep
    over a directory of this project that holds assembly files is source
    navigation and counts."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "grep -n foo src/",
        "grep -rn foo docs/",
        "grep -rn foo --include='*.c' src/",
        "rg -t py foo",
        "grep -rn foo /tmp",
        "git status | grep -r main",
    ],
)
def test_directory_greps_that_do_not_touch_assembly_are_not_counted(hook: Hook, command: str):
    (hook.cwd / "docs").mkdir()
    (hook.cwd / "docs" / "notes.md").write_text("x")
    assert not any(d.denied for d in hook.burst(command, 4)), command


@pytest.mark.parametrize(
    "command",
    [
        "for f in src/*.s; do cat $f; done",
        'for f in src/*.s; do cat "$f"; done',
        "if true; then cat src/main.s; fi",
        "time grep -n jsr src/main.s",
        "env LC_ALL=C grep -n jsr src/main.s",
        "nice cat src/main.s",
        "command cat src/main.s",
    ],
)
def test_shell_keywords_and_wrappers_before_the_command(hook: Hook, command: str):
    """The classifier skips leading keywords/wrappers (`do cat`, `then cat`,
    `time grep`, `env … grep`) before reading the command name; the live sweep
    of 2026-09-01 only registered because its grep followed a `;`."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "( cat src/main.s )",
        "{ cat src/main.s; }",
        "LC_ALL=C grep -n foo src/main.s",
        "timeout 10 cat src/main.s",
        "timeout -k 5 10 cat src/main.s",
        "\\cat src/main.s",
        "x=$(cat src/main.s)",
        'x=`cat src/main.s`; echo "$x"',
        'echo "$(sed -n 1,40p src/main.s)"',
        "cat<src/main.s",
        "cat < src/main.s",
        "if grep -q jsr src/main.s; then echo y; fi",
        "sudo cat src/main.s",
        "f=src/main.s; cat $f",
        "cat src/main.s 2>/dev/null",
    ],
)
def test_more_wrappers_and_shell_forms(hook: Hook, command: str):
    """F10, extending test_shell_keywords_and_wrappers_before_the_command."""
    assert hook.burst(command, 3)[-1].denied, command


def test_read_loop_over_a_list_file_is_not_counted_by_decision(hook: Hook):
    """F10 leftover, decided 2026-09-02: NOT counted. `$f` is bound by `read`
    and `list.txt` is not assembly, so the hook cannot know what is read
    without executing the loop. Counting `cat $UNKNOWN` would reopen the
    false positives (`cat $LOG`) that blocked real work, so the operand rule
    wins. Pinned green as a documented decision."""
    assert not any(d.denied for d in hook.burst("while read f; do cat $f; done < list.txt", 4))


@pytest.mark.parametrize(
    "command",
    [
        "find src -name '*.s' | xargs cat",
        "find src -name '*.s' -exec cat {} +",
        "find . -name '*.inc' -exec grep -n foo {} \\;",
        "ls src/*.s | xargs grep -n foo",
        "git grep -n foo -- '*.s'",
        "git grep -n foo",
        "git show HEAD:src/main.s",
        "git -C . show HEAD~1:src/main.s",
        "diff src/main.s src/old.s",
        "diff -u src/main.s src/lib.inc | head",
    ],
)
def test_other_readers_count(hook: Hook, command: str):
    """F11 (the unambiguous subset)."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"print(open('src/main.s').read())\"",
        "python3 - <<'EOF'\nprint(open('src/main.s').read())\nEOF",
        "bash -c 'cat src/main.s'",
        "eval cat src/main.s",
        "od -c src/main.s",
        "strings src/main.s",
        "cut -c1-40 src/main.s",
        "find src -name '*.c' -exec cat {} +",
        "find src -name '*.s' | xargs wc -l",
        "git show HEAD --stat",
        "git diff src/main.s",
    ],
)
def test_readers_left_uncounted_by_decision(hook: Hook, command: str):
    """F11 remainder, decided 2026-09-02: interpreters, `eval`, binary dumpers
    and `cut` stay uncounted. They are rare in CA65 sessions and counting them
    would mean parsing program text, which is where false positives live."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


@pytest.mark.parametrize(
    "command",
    [
        "grep -q jsr src/main.s && make",
        "grep -c jsr src/main.s",
        "grep -l jsr src/*.s",
        "head -c 0 src/main.s",
        "sed 's/x/y/' src/main.s > build/main_gen.s",
    ],
)
def test_debatable_readers_still_count(hook: Hook, command: str):
    """F07, decided 2026-09-02: a grep that only counts or tests, or a read
    whose output goes elsewhere, still opens an assembly file to answer a
    question the symbolic tools could answer. Kept counted; revisit if it
    misfires live."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "cat src/{main,util}.s",
        "cat src/MAIN.S",
        "cat src/*.{s,inc}",
        "cat 'src/main.s'",
        'cat "src/main.s"',
        "cat ./src/../src/main.s",
    ],
)
def test_path_spellings(hook: Hook, command: str):
    """F12: ca65-ls itself lowercases suffixes, so `.S` is a source file;
    brace expansion and quoting are ordinary shell."""
    assert hook.burst(command, 3)[-1].denied, command


@pytest.mark.parametrize(
    "command",
    [
        "cat $SRC/main.s",
        "cat ${f}",
        "cat $f.s",
        "cat src/main.s.$ext",
    ],
)
def test_unresolvable_variables_are_not_counted(hook: Hook, command: str):
    """A path the hook cannot place inside or outside the project is not
    counted; the conservative side is the silent one."""
    assert not any(d.denied for d in hook.burst(command, 4)), command


# ============================================================ GREEN: classifier unit tests


def test_tokenizer_respects_quotes_and_operators(nudge):
    toks = nudge._tokenize("cd src && grep -n 'a;b' \"c|d\" main.s 2>&1 | head\ncat<x.s")
    assert toks == [
        (0, "cd"),
        (0, "src"),
        (1, "&&"),
        (0, "grep"),
        (0, "-n"),
        (0, "a;b"),
        (0, "c|d"),
        (0, "main.s"),
        (0, "2"),
        (1, ">&"),
        (0, "1"),
        (1, "|"),
        (0, "head"),
        (1, "\n"),
        (0, "cat"),
        (1, "<"),
        (0, "x.s"),
    ]


def test_heredoc_stripping(nudge):
    cmd = "cat <<'EOF' > a\nsrc/main.s\nEOF\ncat <<-TAG\n\tsrc/lib.inc\n\tTAG\necho $((1<<3))\ncat src/x.s"
    assert nudge._strip_heredocs(cmd) == "cat <<'EOF' > a\ncat <<-TAG\necho $((1<<3))\ncat src/x.s"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat src/main.s", "read"),
        ("grep -rn foo src", "grep"),
        ("cat src/main.s; grep -n foo src/lib.inc", "grep"),
        ("cat README.md", None),
        ("grep -rn foo /tmp", None),
        ("cat > out.md <<EOF\nsrc/main.s\nEOF", None),
        ("for r in src; do grep -rn foo $r; done", "grep"),
        ("cd src; cat main.s", "read"),
    ],
)
def test_classifier_kinds(classify, command, expected):
    assert classify(command) == expected


def test_huge_command_is_classified_quickly(hook: Hook):
    """F21: the classifier must be linear; a 100 kB heredoc used to stall the
    hook for 21 s (and the 600 s hook timeout would then allow the call)."""
    blob = "a" * 100_000
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
        raise AssertionError("hook took longer than 1 s on a 100 kB command") from None
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "command",
    [
        "cat > out.txt <<'EOF'\n" + "a" * 100_000 + "\nEOF",
        "cat > out.txt <<'EOF'\n" + "a.b" * 34_000 + "\nEOF",
        "echo " + "A" * 100_000,
        "echo " + "'" + "x" * 100_000,
        "cat src/main.s " + "src/main.s " * 9_000,
        "cat " + "{a,b}" * 20_000 + ".s",
    ],
    ids=[
        "heredoc-words",
        "heredoc-dotted",
        "single-word",
        "unterminated-quote",
        "many-operands",
        "brace-bomb",
    ],
)
def test_classifier_itself_is_fast_on_100kb(classify, command):
    start = time.perf_counter()
    classify(command)
    assert time.perf_counter() - start < 0.1


# ============================================================ GREEN: robustness


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "{}",
        "[]",
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": ["cat", "main.s"]}}),
        json.dumps({"tool_name": None, "tool_input": None, "session_id": None, "cwd": None}),
        json.dumps({"tool_name": "Bash", "tool_input": "cat main.s", "session_id": "s", "cwd": 3}),
    ],
)
def test_malformed_payloads_never_block(hook: Hook, raw: str):
    d = hook.call("Bash", raw=raw)
    assert d.returncode == 0 and not d.denied


@pytest.mark.parametrize("session", [None, "", 0])
def test_missing_session_id_writes_no_state(hook: Hook, session):
    """F19: without a session id there is nothing to count against; the old
    shared `default.json` mixed unrelated callers into one counter."""
    payload = {"cwd": str(hook.cwd), "tool_name": "Bash", "tool_input": {"command": READ}}
    if session is not None:
        payload["session_id"] = session
    for _ in range(4):
        assert not hook.call("Bash", raw=json.dumps(payload)).denied
    assert not (hook.home / ".claude" / "ca65-bash-nudge").exists()


def test_corrupt_state_file_is_tolerated(hook: Hook):
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.write_text("{not json")
    assert not any(d.denied for d in hook.burst(READ, 2))
    assert hook.bash(READ).denied


@pytest.mark.parametrize(
    "state",
    [
        {"last_deny": 4102444800.0},
        {"count": "abc", "last_seen": None},
        {"count": True},
        {"count": -5},
        {"last_seen": "yesterday"},
        {"count": 1, "last_deny": float("inf")},
    ],
    ids=[
        "future-last_deny",
        "bad-count-in-live-burst",
        "bool-count",
        "negative-count",
        "string-time",
        "inf-time",
    ],
)
def test_state_that_survives_load_still_recovers(hook: Hook, state: dict):
    """F18: a `last_deny` in the future used to keep the quiet window open for
    the rest of the session; a non-integer count with a live `last_seen`
    raised on every call and was swallowed. Invalid state is rewritten."""
    if state.get("last_seen", 0) is None:
        state["last_seen"] = time.time()
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.write_text(json.dumps(state))
    assert any(d.denied for d in hook.burst(READ, 3)), "hook went silent for the session"
    assert isinstance(hook.state()["count"], int)


@pytest.mark.parametrize("state", ['{"count": "abc"}', '{"count": null}', '{"count": [1]}', "[]"])
def test_non_integer_count_without_last_seen_recovers(hook: Hook, state: str):
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.write_text(state)
    assert hook.burst(READ, 3)[-1].denied


def test_old_state_files_are_pruned(hook: Hook):
    """F20: one file per session forever. Files untouched for a day go."""
    state_dir = hook.state_path.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    stale = state_dir / "dead-session.json"
    fresh = state_dir / "live-session.json"
    stale.write_text("{}")
    fresh.write_text("{}")
    old = time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))
    hook.bash(READ)
    assert not stale.exists()
    assert fresh.exists()
    assert hook.state_path.exists()


def test_deny_output_matches_claude_code_contract(hook: Hook):
    out = json.loads(hook.burst(READ, 3)[-1].stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert isinstance(hso["permissionDecisionReason"], str) and hso["permissionDecisionReason"]
    # F22: the reason reaches the model on a deny by itself; do not repeat it.
    assert "additionalContext" not in hso
    assert set(out) == {"hookSpecificOutput"}


def test_hook_is_executable_and_has_shebang():
    assert HOOK.stat().st_mode & 0o111, "settings.json runs the script directly"
    assert HOOK.read_text().startswith("#!/usr/bin/env python3")


def test_hook_exits_zero_on_internal_error(hook: Hook, monkeypatch):
    # Unwritable state dir must not turn into a blocked tool call.
    hook.state_path.parent.mkdir(parents=True, exist_ok=True)
    hook.state_path.mkdir()  # a directory where the file should be
    d = hook.bash(READ)
    assert d.returncode == 0 and not d.denied


def test_allowed_calls_print_nothing(hook: Hook):
    d = hook.bash(READ)
    assert d.stdout == "" and d.returncode == 0


@pytest.mark.parametrize(
    "command", ["sed -i.bak 's/a/b/' src/main.s", "tee notes.md <<'EOF'\nsee src/main.s\nEOF"]
)
def test_already_handled_edit_forms(hook: Hook, command: str):
    assert not any(d.denied for d in hook.burst(command, 4)), command
