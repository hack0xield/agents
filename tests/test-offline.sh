#!/usr/bin/env bash
# Verify mt5.* degrades honestly when the terminal is unreachable.
#
# Points the client at a dead port rather than stopping the real bridge, so
# this is safe to run against a live system.
#
# This is the failure that matters most now that the fixtures are gone: the
# tools must report DISCONNECTED, never an empty account. "No open positions"
# and "I cannot see your account" mean opposite things to a trader.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MT5_BRIDGE_URL="http://127.0.0.1:9" MT5_TIMEOUT=2 \
"$REPO/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, "mcp_server")
import mt5_live

fails = 0

acc = mt5_live.get_account()
if acc.get("connection_state") == "DISCONNECTED" and "balance" not in acc:
    print("  ok   get_account -> DISCONNECTED, no invented balance")
else:
    print(f"  FAIL get_account returned {acc}"); fails += 1

pos = mt5_live.get_positions()
if isinstance(pos, dict) and pos.get("connection_state") == "DISCONNECTED":
    print("  ok   get_positions -> DISCONNECTED, not an empty list")
else:
    print(f"  FAIL get_positions returned {pos!r} — an empty list here would "
          f"read as 'you are flat'"); fails += 1

hist = mt5_live.get_trade_history(limit=5)
if isinstance(hist, dict) and hist.get("connection_state") == "DISCONNECTED":
    print("  ok   get_trade_history -> DISCONNECTED, not an empty list")
else:
    print(f"  FAIL get_trade_history returned {hist!r}"); fails += 1

st = mt5_live.status()
if st.get("connection_state") == "DISCONNECTED":
    print("  ok   status -> DISCONNECTED")
else:
    print(f"  FAIL status returned {st}"); fails += 1

print("\nFAILED" if fails else "\nAll green — offline degrades honestly")
sys.exit(1 if fails else 0)
PY
