#!/usr/bin/env bash
# Launch the local Claude Code CLI shim that backs the `claude-code-cli`
# Hermes provider. Keep this running while you use the provider.
#
#   ./start.sh                # serve on 127.0.0.1:8765
#   CLAUDE_CODE_CLI_PORT=9000 ./start.sh
#   CLAUDE_CODE_CLI_TOOLS=Read,Bash ./start.sh   # let the CLI use tools
#
# See README.md for all environment-variable overrides.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: python3 not found on PATH (set \$PYTHON to override)" >&2
  exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/claude_code_server.py"
