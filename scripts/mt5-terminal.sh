#!/usr/bin/env bash
# Keep one MT5 terminal running, in the foreground, for systemd.
#
# `wine terminal64.exe` cannot be supervised directly. Once MT5 has applied a
# LiveUpdate it re-execs itself as `terminal64.exe /skipupdate:<token>
# /portable` and the launching wine exits 0 after ~2 s, so Type=simple sees the
# service die. Type=forking does not help either: MT5 writes no PID file, and
# systemd cannot guess a main PID from a cgroup holding wineserver, two
# winedevice processes and the terminal.
#
# So: launch it, find it, and block until it is gone. The script's lifetime
# then equals the terminal's, which is exactly what Restart=always needs.
set -uo pipefail
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG="${WINEDEBUG:--all}"
TERMINAL='C:\Program Files\MetaTrader 5\terminal64.exe'

# The bracket keeps the pattern from matching this script's own command line.
running() { pgrep -f 'terminal64[.]exe' 2>/dev/null | head -1; }

if [ -z "$(running)" ]; then
  echo "[mt5] launching terminal"
  wine "$TERMINAL" >/dev/null 2>&1 || true      # returns immediately; expected
fi

for _ in $(seq 1 60); do
  PID="$(running)"
  [ -n "$PID" ] && break
  sleep 1
done

if [ -z "${PID:-}" ]; then
  echo "[mt5] terminal did not appear after 60s" >&2
  exit 1
fi

echo "[mt5] terminal running as pid $PID"
# Poll rather than `wait`: it is not our child, wine detached it.
while kill -0 "$PID" 2>/dev/null; do sleep 5; done
echo "[mt5] terminal exited" >&2
