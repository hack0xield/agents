#!/usr/bin/env bash
# Telegram notifications for the live traders.
#
#   scripts/live-notify.sh run                  the service
#   scripts/live-notify.sh status --mock        preview a status from fixtures
#   scripts/live-notify.sh events --mock        preview every event message
#
# See apps/live_notify/main.py for --send-to and --send-all.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO/.env" ]; then
  set -a; source "$REPO/.env"; set +a
fi
exec "$REPO/.venv/bin/python" "$REPO/apps/live_notify/main.py" "$@"
