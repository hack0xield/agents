#!/usr/bin/env bash
# Run the read-only MT5 bridge under the Wine Python that shares the MT5 prefix.
# MetaTrader5 is Windows-only, so this cannot run on the Linux interpreter.
#
# Deliberately NOT `exec wine …`. exec replaces this shell, leaving nothing to
# handle Ctrl-C — and Wine does not reliably deliver SIGINT to the Windows-side
# Python, so the terminal appears to hang. Keeping bash as the parent means the
# trap below can stop it.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1

cd "$REPO/mt5_bridge"

stop() {
  echo
  echo "[bridge] stopping…"
  # Kill the wine wrapper, then the Windows-side interpreter it left behind.
  # The MT5 terminal is intentionally left running: it is shared, slow to
  # start, and not ours to close.
  [ -n "${WINE_PID:-}" ] && kill "$WINE_PID" 2>/dev/null
  for p in $(pgrep -f 'python\.exe .*bridge\.py' 2>/dev/null); do
    kill "$p" 2>/dev/null
  done
  sleep 1
  for p in $(pgrep -f 'python\.exe .*bridge\.py' 2>/dev/null); do
    kill -9 "$p" 2>/dev/null
  done
  echo "[bridge] stopped (MT5 terminal left running)"
  exit 0
}
trap stop INT TERM

wine 'C:\Python311\python.exe' bridge.py &
WINE_PID=$!
wait "$WINE_PID"
