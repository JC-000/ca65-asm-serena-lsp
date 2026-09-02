"""Harness for driving scripts/ca65_bash_nudge.py exactly as Claude Code does.

The hook is a stdin/stdout subprocess, so every test spawns it with a private
HOME (its state dir is ``$HOME/.claude/ca65-bash-nudge``) and a throwaway
project directory. Nothing here touches the live state under ``~/.claude``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "ca65_bash_nudge.py"

CA65_YML = "project_name: p\nlanguages:\n- python\n- ca65\nencoding: utf-8\n"
CA65_YML_NEW_KEY = "project_name: p\nlanguage_servers:\n  - ca65\nencoding: utf-8\n"
PYTHON_ONLY_YML = "project_name: p\nlanguages:\n- python\nencoding: utf-8\n"


@dataclass
class Decision:
    """What the hook told Claude Code for one call."""

    stdout: str
    returncode: int

    @property
    def denied(self) -> bool:
        return self.output is not None and self.output.get("permissionDecision") == "deny"

    @property
    def output(self) -> dict | None:
        if not self.stdout.strip():
            return None
        return json.loads(self.stdout)["hookSpecificOutput"]

    @property
    def reason(self) -> str:
        assert self.output is not None, "no decision was emitted"
        return self.output["permissionDecisionReason"]


class Hook:
    """One simulated Claude Code session talking to the hook."""

    def __init__(self, home: Path, cwd: Path, session_id: str | None = None):
        self.home = home
        self.cwd = cwd
        self.session_id = session_id or f"test-{uuid.uuid4()}"

    # -- driving ---------------------------------------------------------

    def call(
        self,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        cwd: Path | None = None,
        raw: str | None = None,
    ) -> Decision:
        payload = raw
        if payload is None:
            payload = json.dumps(
                {
                    "session_id": self.session_id,
                    "cwd": str(cwd or self.cwd),
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input if tool_input is not None else {},
                }
            )
        env = dict(os.environ, HOME=str(self.home))
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return Decision(stdout=proc.stdout, returncode=proc.returncode)

    def bash(self, command: str, *, cwd: Path | None = None) -> Decision:
        return self.call("Bash", {"command": command}, cwd=cwd)

    def symbolic(self, tool: str = "find_symbol") -> Decision:
        return self.call(f"mcp__serena__{tool}", {"name_path_pattern": "x"})

    def burst(self, command: str, n: int, **kw) -> list[Decision]:
        return [self.bash(command, **kw) for _ in range(n)]

    # -- state -----------------------------------------------------------

    @property
    def state_path(self) -> Path:
        # Mirrors _state_path in the hook: session id sanitised to a filename.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.session_id) or "default"
        return self.home / ".claude" / "ca65-bash-nudge" / f"{safe}.json"

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}

    def set_state(self, **fields) -> None:
        state = self.state()
        state.update(fields)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))

    def age_last_deny(self, seconds: float) -> None:
        """Pretend the last deny happened `seconds` ago."""
        self.set_state(last_deny=time.time() - seconds)

    def age_last_seen(self, seconds: float) -> None:
        self.set_state(last_seen=time.time() - seconds)


def make_project(
    root: Path,
    name: str,
    yml: str | None,
    asm_files: tuple[str, ...] = ("src/main.s", "src/lib.inc"),
) -> Path:
    project = root / name
    for rel in asm_files:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(".proc start\n  rts\n.endproc\n")
    if yml is not None:
        (project / ".serena").mkdir(parents=True, exist_ok=True)
        (project / ".serena" / "project.yml").write_text(yml)
    return project


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def ca65_project(tmp_path: Path) -> Path:
    return make_project(tmp_path, "c64-thing", CA65_YML)


@pytest.fixture
def other_ca65_project(tmp_path: Path) -> Path:
    return make_project(tmp_path, "c64-other", CA65_YML)


@pytest.fixture
def python_project(tmp_path: Path) -> Path:
    return make_project(tmp_path, "py-thing", PYTHON_ONLY_YML)


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    return make_project(tmp_path, "bare", None)


@pytest.fixture
def hook(home: Path, ca65_project: Path) -> Hook:
    return Hook(home, ca65_project)
