#!/usr/bin/env bash
# install_local_serena.sh -- enable our CA65-aware Serena fork for testing.
#
# Default mode: PROJECT-scoped (writes <project>/.mcp.json).  Only that one
# project gets the forked Serena; all your other Claude Code sessions keep
# using the upstream Serena.  Recommended for testing.
#
# Use --global to install at user scope (rewrites ~/.claude.json).  Affects
# every Claude Code session everywhere.  Useful if/when M4 stabilizes and you
# want CA65 support across all eight c64-* projects.
#
# Usage:
#     install_local_serena.sh                           # project: c64-https (default)
#     install_local_serena.sh --project /path/to/proj   # project: arbitrary path
#     install_local_serena.sh --global                  # user-global override
#     install_local_serena.sh --print                   # dry-run, just print the JSON
#
# What it actually writes:
#   project mode -> <project>/.mcp.json with one MCP entry: `serena`, pointing
#                   at JC-000/serena@feature/ca65-language-server, with --with
#                   for the local ca65-ls source.
#   global mode  -> rewrites ~/.claude.json's `.mcpServers.serena`. Backs up
#                   the original to ~/.claude.json.pre-ca65-<timestamp>.
#
# Reverting:
#   project mode -> rm <project>/.mcp.json   (or git restore if committed)
#   global mode  -> cp ~/.claude.json.pre-ca65-<timestamp> ~/.claude.json
#
# After running:  restart Claude Code.  When it spawns the MCP server for the
# scoped project, you'll be prompted to approve the new `serena` entry the
# first time (project mode only).

set -euo pipefail

CA65_LS_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/packages/ca65-ls"
FORK_URL="git+https://github.com/JC-000/serena@feature/ca65-language-server"
DEFAULT_PROJECT="${HOME}/Documents/c64-https"

MODE="project"
PROJECT="${DEFAULT_PROJECT}"
PRINT_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            MODE="project"
            PROJECT="$2"
            shift 2
            ;;
        --global)
            MODE="global"
            shift
            ;;
        --print)
            PRINT_ONLY=1
            shift
            ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg $1" >&2
            exit 2
            ;;
    esac
done

command -v jq >/dev/null 2>&1 || { echo "ERROR: install jq (brew install jq)" >&2; exit 1; }
command -v uvx >/dev/null 2>&1 || { echo "ERROR: install uv (brew install uv)" >&2; exit 1; }
[[ -d "${CA65_LS_PATH}" ]] || { echo "ERROR: ${CA65_LS_PATH} not found" >&2; exit 1; }

# The JSON describing our forked serena entry.
#
# --with-editable (NOT plain --with): editable-installs the local ca65-ls
# source into uvx's temp environment.  This is critical for dev iteration: a
# plain --with does a regular pip install and caches the wheel, so changes in
# packages/ca65-ls/ wouldn't propagate to a running Claude Code session even
# after restart.  With --with-editable, the env points back at the source dir,
# so every restart picks up the latest code automatically.
ENTRY_JSON=$(jq -n \
    --arg fork "${FORK_URL}" \
    --arg ca65 "${CA65_LS_PATH}" \
    '{
        type: "stdio",
        command: "uvx",
        args: ["--from", $fork, "--with-editable", $ca65, "serena", "start-mcp-server"],
        env: {}
    }')

if [[ ${PRINT_ONLY} -eq 1 ]]; then
    echo "Mode: ${MODE}${MODE:+ (target: ${PROJECT})}"
    echo "Entry:"
    echo "${ENTRY_JSON}" | jq .
    exit 0
fi

if [[ "${MODE}" == "project" ]]; then
    [[ -d "${PROJECT}" ]] || { echo "ERROR: ${PROJECT} is not a directory" >&2; exit 1; }
    MCP_JSON="${PROJECT}/.mcp.json"

    echo "==> Writing ${MCP_JSON}"
    if [[ -f "${MCP_JSON}" ]]; then
        # Preserve any other MCP entries already in the project.
        TMP="$(mktemp)"
        jq --argjson e "${ENTRY_JSON}" '.mcpServers.serena = $e' "${MCP_JSON}" > "${TMP}"
        mv "${TMP}" "${MCP_JSON}"
    else
        jq -n --argjson e "${ENTRY_JSON}" '{mcpServers: {serena: $e}}' > "${MCP_JSON}"
    fi

    jq . "${MCP_JSON}"
    echo
    echo "==> Done (project scope).  Only Claude Code sessions in ${PROJECT}"
    echo "    will use the forked Serena; everywhere else keeps upstream."
    echo
    echo "Next:"
    echo "  1. Restart Claude Code."
    echo "  2. When it opens ${PROJECT}, you'll be prompted to approve the"
    echo "     new \`serena\` MCP server -- approve it."
    echo "  3. First cold start is ~30-60s while uvx clones the fork and"
    echo "     installs ca65-ls; subsequent starts are cached and fast."
    echo
    echo "Revert:"
    echo "  rm ${MCP_JSON}"
    echo "  (or 'git restore' if you've committed it)"
else
    # --global mode: rewrite ~/.claude.json's top-level mcpServers.serena.
    CLAUDE_JSON="${HOME}/.claude.json"
    [[ -f "${CLAUDE_JSON}" ]] || { echo "ERROR: ${CLAUDE_JSON} not found" >&2; exit 1; }

    BACKUP="${CLAUDE_JSON}.pre-ca65-$(date +%Y%m%dT%H%M%S)"
    echo "==> Backing up ${CLAUDE_JSON} -> ${BACKUP}"
    cp "${CLAUDE_JSON}" "${BACKUP}"

    TMP="$(mktemp)"
    jq --argjson e "${ENTRY_JSON}" '.mcpServers.serena = $e' "${CLAUDE_JSON}" > "${TMP}"
    mv "${TMP}" "${CLAUDE_JSON}"

    jq '.mcpServers.serena' "${CLAUDE_JSON}"
    echo
    echo "==> Done (user-global scope).  All Claude Code sessions now use the"
    echo "    forked Serena."
    echo
    echo "Revert:  cp ${BACKUP} ${CLAUDE_JSON}"
fi
