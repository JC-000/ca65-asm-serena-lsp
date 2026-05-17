#!/usr/bin/env bash
# install_local_serena.sh -- swap Claude Code's Serena MCP for our forked
# version with CA65 support.
#
# What it does:
#   1. Backs up ~/.claude.json to ~/.claude.json.pre-ca65-<timestamp>
#   2. Rewrites the `serena` entry under `.mcpServers` to:
#        - launch from JC-000/serena @ feature/ca65-language-server  (our fork)
#        - --with ca65-ls (installed from the local source at packages/ca65-ls)
#   3. Prints what changed and how to revert.
#
# After running: restart Claude Code so the MCP server is re-spawned with the
# new args.  Then activate any CA65 project (e.g. c64-https) and Serena's
# symbolic tools will pick up *.s / *.asm / *.inc files via ca65-ls.
#
# Revert:  cp ~/.claude.json.pre-ca65-<timestamp> ~/.claude.json && restart.

set -euo pipefail

CLAUDE_JSON="${HOME}/.claude.json"
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
BACKUP="${CLAUDE_JSON}.pre-ca65-${TIMESTAMP}"

CA65_LS_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/packages/ca65-ls"
FORK_URL="git+https://github.com/JC-000/serena@feature/ca65-language-server"

if [[ ! -f "${CLAUDE_JSON}" ]]; then
    echo "ERROR: ${CLAUDE_JSON} not found." >&2
    exit 1
fi

if [[ ! -d "${CA65_LS_PATH}" ]]; then
    echo "ERROR: ${CA65_LS_PATH} not found." >&2
    exit 1
fi

# Sanity check: is jq available?  We need it for the JSON edit.
if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required.  Install with:  brew install jq" >&2
    exit 1
fi

# Sanity check: is uvx available?  We need it to launch the rewritten command.
if ! command -v uvx >/dev/null 2>&1; then
    echo "ERROR: uvx is required (part of uv).  Install with:  brew install uv" >&2
    exit 1
fi

echo "==> Backing up ${CLAUDE_JSON}"
cp "${CLAUDE_JSON}" "${BACKUP}"
echo "    -> ${BACKUP}"

echo "==> Confirming current serena entry"
jq '.mcpServers.serena // "MISSING"' "${CLAUDE_JSON}"

echo
echo "==> Rewriting .mcpServers.serena to use our fork"
TMP="$(mktemp)"
jq \
    --arg fork "${FORK_URL}" \
    --arg ca65 "${CA65_LS_PATH}" \
    '.mcpServers.serena = {
        type: "stdio",
        command: "uvx",
        args: [
            "--from", $fork,
            "--with", $ca65,
            "serena", "start-mcp-server"
        ],
        env: (.mcpServers.serena.env // {})
    }' "${CLAUDE_JSON}" > "${TMP}"

mv "${TMP}" "${CLAUDE_JSON}"

echo "==> New serena entry:"
jq '.mcpServers.serena' "${CLAUDE_JSON}"

echo
echo "==> Done."
echo
echo "Next steps:"
echo "  1. Restart Claude Code (or any MCP host using ~/.claude.json) so the"
echo "     serena subprocess is re-spawned with the new args."
echo "  2. The first cold start will be slow (~30-60s) while uvx clones the"
echo "     fork and installs ca65-ls.  Subsequent starts are fast (cached)."
echo "  3. Activate any CA65 project (e.g. /Users/someone/Documents/c64-https)"
echo "     and Serena's symbolic tools will use ca65-ls for *.s/*.asm/*.inc."
echo
echo "To revert:"
echo "  cp ${BACKUP} ${CLAUDE_JSON}"
echo "  (then restart Claude Code)"
