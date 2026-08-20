"""Client for the read-only MT5 bridge.

The bridge runs under Wine (see mt5_bridge/bridge.py) because MetaTrader5 is
Windows-only. This module talks to it over loopback HTTP and is the only place
the MCP server knows a broker exists.

Live only, and per account. Every call names a `credential_ref` identifying
whose account to read; there is no default. A request without one returns
NO_ACCOUNT rather than whichever account the terminal happens to be on — that
fallback is how one user ends up reading another's positions.

The ref comes from the caller's `trading_accounts` row, never from the model.

That is deliberate. Quietly substituting stub data for a real account is the
worst failure this system could have — a trader told about positions they do
not hold, or reassured about a balance that is not theirs. Keeping a plausible
fake around is what makes that failure possible, so the fake is gone rather
than merely switched off.

Every payload carries `data_source`, so the agent can state what it is looking
at without inferring.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

BRIDGE = os.environ.get("MT5_BRIDGE_URL", "http://127.0.0.1:8082")
TIMEOUT = float(os.environ.get("MT5_TIMEOUT", "20"))


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


def _no_account() -> dict:
    return {
        "connection_state": "NO_ACCOUNT",
        "data_source": "none",
        "error": "no trading account connected for this user",
        "hint": "Connect an MT5 account before asking about account data.",
    }


def _rows_or_error(path: str) -> list | dict:
    data, err = _get(path)
    if err or data is None:
        return _disconnected(err or "no data")
    if isinstance(data, dict) and "error" in data:
        return _disconnected(str(data.get("detail", data["error"])))
    return [{**row, "data_source": "live"} for row in data]


def get_account(credential_ref: str | None = None) -> dict:
    if not credential_ref:
        return _no_account()
    data, err = _get(f"/account?ref={credential_ref}")
    if err or data is None or "error" in data:
        detail = err or str(data.get("detail", data.get("error")))
        return _disconnected(detail)
    return {**data, "data_source": "live"}


def get_positions(credential_ref: str | None = None) -> list | dict:
    if not credential_ref:
        return _no_account()
    return _rows_or_error(f"/positions?ref={credential_ref}")


def get_trade_history(limit: int = 20, symbol: str | None = None,
                      days: int = 90, credential_ref: str | None = None) -> list | dict:
    if not credential_ref:
        return _no_account()
    q = f"/history?ref={credential_ref}&days={days}" + (f"&symbol={symbol}" if symbol else "")
    rows = _rows_or_error(q)
    return rows[-limit:] if isinstance(rows, list) else rows


def status(credential_ref: str | None = None) -> dict:
    data, err = _get("/health")
    if err or data is None:
        return {"connection_state": "DISCONNECTED", "detail": err}
    out = dict(data)
    out["has_account"] = bool(credential_ref)
    return out
