#!/usr/bin/env bash
# regen_fixtures.sh -- assembles and links the synthetic CA65 test corpus,
# producing committed fixture artifacts (.dbg, .lbl, .map) used by the
# dbg_oracle, indexer tests, and Serena integration tests.
#
# Usage:  bash tools/regen_fixtures.sh
# Requires:  ca65, ld65 on PATH (brew install cc65).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURES="${REPO_ROOT}/tests/fixtures/test_repo"

cd "${FIXTURES}"

# Clean
rm -rf build
mkdir -p build

# Assemble (with debug info)
for src in src/zp.s src/lib.s src/helpers.s src/main.s; do
    obj="build/$(basename "${src%.s}").o"
    echo "ca65 -g  ${src}  ->  ${obj}"
    ca65 -g -I inc -o "${obj}" "${src}"
done

# Link, requesting every flavor of symbol output we care about:
#   --dbgfile      structured debug info
#   -Ln <file>     VICE-style label file
#   -m <file>      human-readable map file
#   -C <cfg>       our minimal linker config
echo "ld65 -> build/test_repo.{prg,dbg,lbl,map}"
ld65 \
    -C cfg/test_repo.cfg \
    --dbgfile build/test_repo.dbg \
    -Ln       build/test_repo.lbl \
    -m        build/test_repo.map \
    -o        build/test_repo.prg \
    build/zp.o build/lib.o build/helpers.o build/main.o

echo
echo "Generated fixtures under ${FIXTURES}/build/:"
ls -lh build/
