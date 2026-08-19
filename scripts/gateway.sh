#!/usr/bin/env bash
# Load .env and start the OpenClaw gateway in the foreground.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/node-env.sh
source "$REPO/scripts/node-env.sh"

if [ ! -f "$REPO/.env" ]; then
  echo "error: $REPO/.env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$REPO/.env"
set +a

for var in TELEGRAM_BOT_TOKEN ANTHROPIC_API_KEY; do
  if [ -z "${!var:-}" ]; then
    echo "error: $var is empty in .env" >&2
    exit 1
  fi
done

exec "$REPO/node_modules/.bin/openclaw" gateway "$@"
