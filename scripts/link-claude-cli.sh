#!/usr/bin/env bash
# Put the Claude Code CLI on PATH as ~/.local/bin/claude.
#
# The only claude binary on this machine ships inside the Cursor extension, at a
# path containing the extension version:
#   ~/.cursor/extensions/anthropic.claude-code-<VERSION>-linux-x64/resources/native-binary/claude
# That path changes every time the extension updates, so this script re-resolves
# the newest one instead of hardcoding it. Re-run it if the CLI backend starts
# failing after a Cursor update.
set -euo pipefail

DEST="$HOME/.local/bin/claude"
mkdir -p "$HOME/.local/bin"

BIN="$(find "$HOME/.cursor/extensions" -maxdepth 4 \
        -path '*anthropic.claude-code-*/resources/native-binary/claude' \
        -type f -executable 2>/dev/null | sort -V | tail -1)"

if [ -z "$BIN" ]; then
  echo "error: no Claude Code binary found under ~/.cursor/extensions" >&2
  echo "       install the Claude Code extension, or install the CLI standalone." >&2
  exit 1
fi

ln -sfn "$BIN" "$DEST"
echo "linked $DEST -> $BIN"
"$DEST" --version
