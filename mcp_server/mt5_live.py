"""Client for the read-only MT5 bridge.

The bridge runs under Wine (see mt5_bridge/bridge.py) because MetaTrader5 is
Windows-only. This module talks to it over loopback HTTP and is the only place
the MCP server knows a broker exists.

Two modes, chosen by MT5_MODE:

    live      (default) — call the bridge. If it is unreachable, say so.
    fixtures            — serve the committed fixture files.

There is deliberately **no silent fallback** from live to fixtures. Quietly
substituting stub data for a real account is the worst failure this system
could have: the trader would be told about positions they do not hold, or
reassured about a balance that is not theirs. A visible DISCONNECTED is a
recoverable annoyance; invented account state is not.

Every payload carries `data_source`, so the agent can say which it is looking
at (TOOLS.md requires it to).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BRIDGE = os.environ.get("MT5_BRIDGE_URL", "http://127.0.0.1:8082")
MODE = os.environ.get("MT5_MODE", "live").lower()
TIMEOUT = float(os.environ.get("MT5_TIMEOUT", "20"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _get(path: str) -> tuple[Any, str | None]:
    """Returns (payload, error). Never raises — a dead bridge is a state to
    report, not an exception for the agent to interpret."""
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=TIMEOUT) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), f"bridge returned {e.code}"
        except Exception:
            return None, f"bridge returned {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _disconnected(detail: str) -> dict:
    return {
        "connection_state": "DISCONNECTED",
        "data_source": "none",
        "error": "MT5 bridge unreachable",
        "detail": detail,
        "hint": "Start it with ./scripts/mt5-bridge.sh",
    }


def get_account() -> dict:
    if MODE == "fixtures":
        return {**_fixture("account.json"), "data_source": "fixture"}
    data, err = _get("/account")
    if err or data is None or "error" in data:
        return _disconnected(err or str(data.get("detail", data.get("error"))))
    return {**data, "data_source": "live"}


def get_positions() -> list | dict:
    if MODE == "fixtures":
        return [{**p, "data_source": "fixture"} for p in _fixture("positions.json")]
    data, err = _get("/positions")
    if err or data is None:
        return _disconnected(err or "no data")
    if isinstance(data, dict) and "error" in data:
        return _disconnected(str(data.get("detail", data["error"])))
    return [{**p, "data_source": "live"} for p in data]


def get_trade_history(limit: int = 20, symbol: str | None = None,
                      days: int = 90) -> list | dict:
    if MODE == "fixtures":
        rows = _fixture("trade_history.json")
        if symbol:
            rows = [r for r in rows if r["symbol"].upper() == symbol.upper()]
        return [{**r, "data_source": "fixture"} for r in rows[-limit:]]

    q = f"/history?days={days}" + (f"&symbol={symbol}" if symbol else "")
    data, err = _get(q)
    if err or data is None:
        return _disconnected(err or "no data")
    if isinstance(data, dict) and "error" in data:
        return _disconnected(str(data.get("detail", data["error"])))
    return [{**r, "data_source": "live"} for r in data[-limit:]]


def status() -> dict:
    if MODE == "fixtures":
        return {"mode": "fixtures", "connection_state": "N/A"}
    data, err = _get("/health")
    if err or data is None:
        return {"mode": "live", "connection_state": "DISCONNECTED", "detail": err}
    return {"mode": "live", **data}
