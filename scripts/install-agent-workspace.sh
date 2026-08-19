#!/usr/bin/env bash
# Install the versioned agent behaviour into the OpenClaw workspace.
#
# The agent's behaviour is product configuration, so it lives in this repo and
# is installed from here — not authored in place by the agent. OpenClaw's own
# workspace is a separate git repo under ~/.openclaw/, which is why these files
# would otherwise be outside version control entirely.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/agent-workspace"
DEST="$HOME/.openclaw/workspace-trading"

if [ ! -d "$DEST" ]; then
  echo "error: $DEST does not exist. Start the gateway once so OpenClaw seeds it." >&2
  exit 1
fi

for f in SOUL.md IDENTITY.md AGENTS.md USER.md TOOLS.md; do
  if [ -f "$DEST/$f" ] && ! cmp -s "$SRC/$f" "$DEST/$f"; then
    cp "$DEST/$f" "$DEST/$f.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  cp "$SRC/$f" "$DEST/$f"
  echo "installed $f"
done

# BOOTSTRAP.md tells the agent to interview the user about its name and vibe,
# then rewrite IDENTITY.md, SOUL.md and USER.md with the answers. That would
# overwrite the product behaviour with improvised personality, so it goes.
# OpenClaw only re-seeds it into a workspace that still looks untouched, and
# ours no longer does.
if [ -f "$DEST/BOOTSTRAP.md" ]; then
  rm "$DEST/BOOTSTRAP.md"
  echo "removed BOOTSTRAP.md (identity is product config, not self-discovery)"
fi

echo
echo "workspace: $DEST"
