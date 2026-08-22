#!/usr/bin/env bash
# Public read-only web view of backtest runs. Separate process from the MCP
# server: this one has no tools in it, which is why it is safe to expose.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO/.env" ]; then
  set -a; source "$REPO/.env"; set +a
fi
cd "$REPO"
exec "$REPO/.venv/bin/python" apps/reports/server.py
