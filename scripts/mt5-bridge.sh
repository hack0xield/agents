#!/usr/bin/env bash
# Run the read-only MT5 bridge under the Wine Python that shares the MT5 prefix.
# MetaTrader5 is Windows-only, so this cannot run on the Linux interpreter.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1
cd "$REPO/mt5_bridge"
exec wine 'C:\Python311\python.exe' bridge.py
