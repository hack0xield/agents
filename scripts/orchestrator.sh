#!/usr/bin/env bash
# Run the orchestrator (our own agent runtime, replacing OpenClaw).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO/.env" ]; then
  set -a; source "$REPO/.env"; set +a
fi
cd "$REPO/apps/orchestrator"
exec "$REPO/.venv/bin/python" main.py
