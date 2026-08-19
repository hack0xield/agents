#!/usr/bin/env bash
# Install the versioned gateway config to ~/.openclaw/openclaw.json.
# Backs up any existing config first — never clobber silently.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/openclaw/openclaw.json5"
DEST_DIR="$HOME/.openclaw"
DEST="$DEST_DIR/openclaw.json"

mkdir -p "$DEST_DIR"

if [ -f "$DEST" ] && ! cmp -s "$SRC" "$DEST"; then
  BACKUP="$DEST.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$DEST" "$BACKUP"
  echo "existing config backed up -> $BACKUP"
fi

cp "$SRC" "$DEST"
echo "installed $SRC -> $DEST"
