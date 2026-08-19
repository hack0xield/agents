#!/usr/bin/env bash
# Run the stub trading MCP server (read-only, fixture-backed).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/python" "$REPO/mcp_server/server.py"
