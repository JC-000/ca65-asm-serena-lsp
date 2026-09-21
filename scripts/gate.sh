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
#   4. through-Serena        packages/ca65-ls/tests/serena  (fork venv), which
#                            also carries the end-to-end parity tests that used
#                            to live in the fork at test/solidlsp/ca65/
#   5. entry-point discovery Serena resolves "ca65" from the installed ca65-ls
#                            package (fork venv)
#
# Usage: scripts/gate.sh [--quick]   (--quick skips 3, the slow corpus run)
#
# Layer 5 replaced the old "fork e2e" layer when CA65 moved out of the Serena
# tree. Serena no longer carries a CA65 enum member or adapter: ca65-ls registers
# itself through the `solidlsp.language_server_registration` entry point, so the
# thing that can now silently break is DISCOVERY, not the shim. It breaks whenever
# ca65-ls is installed with `--no-deps`, reinstalled without refreshing its
# metadata, or pruned by a `uv sync` in the fork — none of which the pytest layers
# would notice, because they import the adapter module directly.

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
  # Runs even under --quick: it is ~8 s and now carries the end-to-end parity
  # tests that --quick used to cover via the old "fork e2e" layer.
  if [ -d "$PKG/tests/serena" ]; then
    run "through-Serena"  "$FORK_PY" -m pytest "$PKG/tests/serena" -q -p no:cacheprovider --rootdir "$PKG"
  fi
  run "entry-point discovery" "$FORK_PY" -c '
import sys
from solidlsp.ls_config import LanguageServerRegistry

registry = LanguageServerRegistry.get_instance()
problems = []

if "ca65" not in registry.get_keys():
    problems.append(
        "Serena does not discover the \"ca65\" key. ca65-ls is not installed into the fork venv, "
        "or its entry-point metadata is stale: reinstall with "
        "`uv pip install -e packages/ca65-ls` (WITHOUT --no-deps)."
    )
else:
    ls_id = registry.resolve("ca65")
    cls = ls_id.get_ls_class()
    if cls.__module__ != "ca65_ls.serena_adapter":
        problems.append(f"\"ca65\" resolves to {cls.__module__}.{cls.__name__}, not the ca65_ls adapter.")
    matcher = ls_id.get_source_fn_matcher()
    for name, expected in (("a.s", True), ("a.asm", True), ("a.inc", True), ("a.py", False)):
        if matcher.is_relevant_filename(name) != expected:
            problems.append(f"matcher misclassifies {name} (expected relevant={expected}).")

# Negative control: a green result must mean discovery really happened, not that
# resolve() hands back a default for anything it is asked.
try:
    bogus = registry.resolve("ca65-not-a-real-key")
except ValueError:
    pass
else:
    problems.append(f"resolve() returned {bogus!r} for an unknown key instead of raising.")

for p in problems:
    print("  " + p, file=sys.stderr)
sys.exit(1 if problems else 0)
'
else
  echo "(skipping fork layers: $FORK_PY not found)"
fi

echo
if [ "$status" = 0 ]; then echo "GATE: all layers green (red tests xfailed as expected)"; else echo "GATE: FAILED"; fi
exit $status
