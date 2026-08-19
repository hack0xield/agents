#!/usr/bin/env bash
# Apply the versioned gateway config to ~/.openclaw/openclaw.json.
#
# Uses `openclaw config patch`, NOT cp. OpenClaw writes to its own config at
# runtime — approving a Telegram pairing adds commands.ownerAllowFrom, for
# example — and a straight copy would silently wipe that state. patch merges
# objects recursively and replaces arrays/scalars, so our file stays the source
# of truth for the keys it declares while runtime-owned keys survive.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/node-env.sh
source "$REPO/scripts/node-env.sh"

# Needed to resolve SecretRef entries in the config (gateway auth token).
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi

SRC="$REPO/openclaw/openclaw.json5"
DEST="$HOME/.openclaw/openclaw.json"
OC="$REPO/node_modules/.bin/openclaw"

mkdir -p "$HOME/.openclaw"

if [ ! -f "$DEST" ]; then
  # No config yet: patch has nothing to merge into, so seed it.
  cp "$SRC" "$DEST"
  echo "seeded $DEST"
else
  cp "$DEST" "$DEST.bak.$(date +%Y%m%d-%H%M%S)"
  "$OC" config patch --file "$SRC"
  echo "patched $DEST (runtime-owned keys preserved)"
fi

"$OC" config validate
