#!/usr/bin/env bash
# Run the read-only trading MCP server.
# backtests.* reads ../trading/runs live; mt5.* proxies the Wine bridge on :8082.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/python" "$REPO/mcp_server/server.py"
