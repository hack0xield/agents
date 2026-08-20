#!/usr/bin/env bash
# Verify mt5.* degrades honestly. Two distinct states, easy to conflate:
#
#   NO_ACCOUNT    this user has no trading account connected
#   DISCONNECTED  they have one, but the terminal is unreachable
#
# Neither may ever come back as an empty list. "No open positions" and "I
# cannot see your account" mean opposite things to a trader, and one of them
# is a lie about their money.
#
# Points the client at a dead port rather than stopping the real bridge, so
# this is safe to run against a live system.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -f .env ]; then set -a; source .env; set +a; fi

MT5_BRIDGE_URL="http://127.0.0.1:9" MT5_TIMEOUT=2 \
"$REPO/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, "mcp_server")
import mt5_live

fails = 0
REF = "founder-demo"          # a ref that exists, so the bridge is reached


def check(name, ok, detail=""):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else ' — ' + str(detail)[:150]}")
    if not ok:
        fails += 1


# --- no account connected: refused before the bridge is even tried
acc = mt5_live.get_account()
check("no ref -> NO_ACCOUNT, no invented balance",
      acc.get("connection_state") == "NO_ACCOUNT" and "balance" not in acc, acc)

pos = mt5_live.get_positions()
check("no ref -> NO_ACCOUNT, not an empty list",
      isinstance(pos, dict) and pos.get("connection_state") == "NO_ACCOUNT", pos)

# --- account connected, terminal unreachable: a different answer
acc = mt5_live.get_account(REF)
check("dead bridge -> DISCONNECTED, no invented balance",
      acc.get("connection_state") == "DISCONNECTED" and "balance" not in acc, acc)

pos = mt5_live.get_positions(REF)
check("dead bridge -> DISCONNECTED, not an empty list",
      isinstance(pos, dict) and pos.get("connection_state") == "DISCONNECTED", pos)

hist = mt5_live.get_trade_history(limit=5, credential_ref=REF)
check("dead bridge -> history DISCONNECTED, not an empty list",
      isinstance(hist, dict) and hist.get("connection_state") == "DISCONNECTED", hist)

st = mt5_live.status(REF)
check("status -> DISCONNECTED", st.get("connection_state") == "DISCONNECTED", st)

# --- the two states must not be the same string
check("NO_ACCOUNT and DISCONNECTED are distinguishable",
      mt5_live.get_account().get("connection_state")
      != mt5_live.get_account(REF).get("connection_state"))

print("\nFAILED" if fails else "\nAll green — offline degrades honestly")
sys.exit(1 if fails else 0)
PY
