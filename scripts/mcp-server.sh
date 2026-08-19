#!/usr/bin/env bash
# Run the read-only trading MCP server.
# backtests.* reads ../trading/runs live; mt5.* proxies the Wine bridge on :8082.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# backtests.send_report needs the bot token and the destination chat.
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi
exec "$REPO/.venv/bin/python" "$REPO/mcp_server/server.py"
