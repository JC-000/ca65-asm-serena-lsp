#!/usr/bin/env bash
# Red/green gate for the CA65 symbolic tooling.
#
# Run this BEFORE and AFTER any change to ca65-ls, the Serena shim, or the Bash
# nudge hook. Every layer must come back green:
#
#   green tests  - behaviour we rely on; a failure is a regression.
#   red tests    - known defects, marked xfail(strict=True); they are expected
#                  to fail. When a fix lands they XPASS, which pytest reports
#                  as a FAILURE: remove the marker (and the RED note in the
#                  docstring) in the same commit as the fix.
#
# Layers, in order:
#   1. hook suite            tests/hook            (repo venv)
#   2. ca65-ls unit tests    packages/ca65-ls/tests, not corpus
#   3. corpus contract       packages/ca65-ls/tests/corpus  against the real
#                            c64-* projects under CA65_CORPUS_ROOT (~/Documents)
#   4. through-Serena        packages/ca65-ls/tests/serena  (fork venv)
#   5. fork e2e              ~/Documents/serena/test/solidlsp/ca65 (fork venv)
#
# Usage: scripts/gate.sh [--quick]   (--quick skips 3 and 4, the slow corpus runs)

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$REPO/packages/ca65-ls"
PY="$PKG/.venv/bin/python"
FORK="${SERENA_FORK:-$HOME/Documents/serena}"
FORK_PY="$FORK/.venv/bin/python"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

status=0
run() {
  local name="$1"; shift
  echo
  echo "==> $name"
  if "$@"; then echo "    ok: $name"; else echo "    FAILED: $name"; status=1; fi
}

[ -x "$PY" ] || { echo "missing venv: $PY (see CLAUDE.md 'Commands')"; exit 2; }

run "hook suite"          "$PY" -m pytest "$REPO/tests/hook" -q -p no:cacheprovider
run "ca65-ls unit tests"  "$PY" -m pytest "$PKG/tests" -q -p no:cacheprovider -m "not corpus" --rootdir "$PKG"

if [ "$QUICK" = 0 ]; then
  run "corpus contract"   "$PY" -m pytest "$PKG/tests/corpus" -q -p no:cacheprovider --rootdir "$PKG"
fi

if [ -x "$FORK_PY" ]; then
  if [ "$QUICK" = 0 ] && [ -d "$PKG/tests/serena" ]; then
    run "through-Serena"  "$FORK_PY" -m pytest "$PKG/tests/serena" -q -p no:cacheprovider --rootdir "$PKG"
  fi
  run "fork e2e"          "$FORK_PY" -m pytest "$FORK/test/solidlsp/ca65" -q -p no:cacheprovider --rootdir "$FORK"
else
  echo "(skipping fork layers: $FORK_PY not found)"
fi

echo
if [ "$status" = 0 ]; then echo "GATE: all layers green (red tests xfailed as expected)"; else echo "GATE: FAILED"; fi
exit $status
