"""Read-only MCP tools for the trading assistant POC.

Stands in for the real MT5 connector and Backtest KB. Every response comes from
a fixture file derived from actual backtest runs (see build_fixtures.py) — no
number here was invented, because a fixture that invents statistics would teach
the agent precisely the habit SOUL.md forbids.

Two guarantees this module exists to enforce, both from spec §10:

  1. There is no tool that can place, modify or close a trade, and none will be
     added. Read-only is a product guarantee backed by the MT5 investor
     password, not a limitation of the stub.

  2. `backtests.*` retrieves stored results. It never runs a backtest (§33).

Run:  .venv/bin/python mcp_server/server.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

FIXTURES = Path(__file__).resolve().parent / "fixtures"

HOST = "127.0.0.1"   # never bind wider: these tools expose account data
PORT = 8081

mcp = MCPServer("trading")

# Declared on every tool. MCP carries read-only as a machine-readable hint, so
# the guarantee travels with the tool definition rather than living only in a
# comment — a client can refuse anything not marked read-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ─────────────────────────────── mt5.* ────────────────────────────────────
# Normalized account state. Broker-specific shapes get flattened here so the
# agent never sees an MT5 quirk (spec §16).

@mcp.tool(name="mt5.get_account", annotations=READ_ONLY)
def mt5_get_account() -> dict:
    """Current state of the connected MT5 account: balance, equity, margin,
    open position count and connection health. Read-only."""
    return _load("account.json")


@mcp.tool(name="mt5.get_positions", annotations=READ_ONLY)
def mt5_get_positions() -> list[dict]:
    """Currently open positions. Returns an empty list when flat — an empty
    result is a real answer, not a failure."""
    return _load("positions.json")


@mcp.tool(name="mt5.get_trade_history", annotations=READ_ONLY)
def mt5_get_trade_history(limit: int = 20, symbol: str | None = None) -> list[dict]:
    """Closed trades, most recent last.

    Args:
        limit: maximum number of trades to return.
        symbol: optional instrument filter, e.g. "XAUUSD".
    """
    rows = _load("trade_history.json")
    if symbol:
        rows = [r for r in rows if r["symbol"].upper() == symbol.upper()]
    return rows[-limit:]


# ──────────────────────────── backtests.* ─────────────────────────────────
# Retrieval only. There is deliberately no tool to run, queue or refresh a
# backtest — spec §33.

@mcp.tool(name="backtests.search", annotations=READ_ONLY)
def backtests_search(
    instrument: str | None = None,
    timeframe: str | None = None,
) -> list[dict]:
    """Find validated backtests matching an instrument and/or timeframe.

    Returns an empty list when nothing matches. An empty list means we have no
    validated evidence for that scenario — it does not mean "estimate it".

    Args:
        instrument: e.g. "XAUUSD". Case-insensitive.
        timeframe: e.g. "H4", "M15". Case-insensitive.
    """
    rows = _load("backtests.json")
    if instrument:
        rows = [r for r in rows if r["instrument"].upper() == instrument.upper()]
    if timeframe:
        rows = [r for r in rows if r["timeframe"].upper() == timeframe.upper()]
    # Search returns identifying fields plus headline metrics only; the full
    # record costs tokens nobody asked for (spec §41).
    return [
        {
            "pattern_id": r["pattern_id"],
            "version": r["version"],
            "instrument": r["instrument"],
            "timeframe": r["timeframe"],
            "sample_size": r["sample_size"],
            "win_rate": r["win_rate"],
            "expectancy_r": r["expectancy_r"],
            "profit_factor": r["profit_factor"],
            "tested_from": r["tested_from"],
            "tested_to": r["tested_to"],
        }
        for r in rows
    ]


@mcp.tool(name="backtests.get_summary", annotations=READ_ONLY)
def backtests_get_summary(pattern_id: str, version: int | None = None) -> dict:
    """Full stored record for one pattern: metrics, conditions, invalidations,
    execution assumptions and stated limitations.

    Returns {"found": false} if the pattern is not in the database.
    """
    for r in _load("backtests.json"):
        if r["pattern_id"].upper() == pattern_id.upper():
            if version is None or r["version"] == version:
                return r
    return {"found": False, "pattern_id": pattern_id, "version": version}


@mcp.tool(name="backtests.get_report", annotations=READ_ONLY)
def backtests_get_report(pattern_id: str, version: int | None = None) -> dict:
    """Locate the stored human-readable report for a pattern.

    Retrieves an existing artifact. It never initiates a new backtest (§33).
    """
    for r in _load("backtests.json"):
        if r["pattern_id"].upper() == pattern_id.upper():
            if version is not None and r["version"] != version:
                continue
            run_id = r["backtest_run_id"]
            return {
                "found": True,
                "pattern_id": r["pattern_id"],
                "version": r["version"],
                "backtest_run_id": run_id,
                "artifacts": {
                    "chart": f"runs/{run_id}/chart.html",
                    "summary": f"runs/{run_id}/summary.json",
                },
                "note": (
                    "POC: artifacts are local paths in the backtester "
                    "workspace, not yet served over HTTP."
                ),
                "limitations": r.get("limitations", []),
            }
    return {"found": False, "pattern_id": pattern_id, "version": version}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=HOST, port=PORT)
