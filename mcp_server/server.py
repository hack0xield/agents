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


def _series_for(run_id: str) -> list[dict]:
    """Which series exist for a run, without shipping the rows themselves."""
    return [
        {
            "series": v["series"],
            "description": v["description"],
            "row_count": v["row_count"],
            "downsampled": v["downsampled"],
        }
        for k, v in _load("series.json").items()
        if k.startswith(run_id + "::")
    ]


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
                    "chart_url": f"http://{HOST}:{PORT}/artifacts/{run_id}/chart.html",
                    "summary_url": f"http://{HOST}:{PORT}/artifacts/{run_id}/summary.json",
                    "bundle_url": f"http://{HOST}:{PORT}/bundle/{run_id}.zip",
                },
                "artifact_note": (
                    "bundle_url is a zip of every file in the run — charts, "
                    "summary and all CSVs. Offer it when someone asks for the "
                    "files themselves. These URLs are served from the machine "
                    "running this assistant: they open in a browser there, and "
                    "are not reachable from a phone or another host. You cannot "
                    "attach or send files, so hand over the link and say where "
                    "it works — never imply you attached anything."
                ),
                "available_series": _series_for(run_id),
                "limitations": r.get("limitations", []),
            }
    return {"found": False, "pattern_id": pattern_id, "version": version}


@mcp.tool(name="backtests.get_series", annotations=READ_ONLY)
def backtests_get_series(
    pattern_id: str,
    series: str,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Underlying data behind a pattern's charts — the numbers a plot is drawn
    from, not the plot image.

    Call `backtests.get_report` first to see which series a pattern has.

    Returns rows plus `row_count` and `returned`, so you can tell a page from
    the whole series. Never claim a total from a page: if `returned` is less
    than `row_count`, you are looking at a slice.

    Args:
        pattern_id: e.g. "EUR_H4_MARGIN_ZONES".
        series: e.g. "envelopes", "pivots", "crossings", "rollover".
        limit: rows to return, capped at 200 — a model reasoning over hundreds
            of raw rows is expensive and rarely more accurate than reasoning
            over the summary.
        offset: rows to skip, for paging through a longer series.
    """
    record = None
    for r in _load("backtests.json"):
        if r["pattern_id"].upper() == pattern_id.upper():
            record = r
            break
    if record is None:
        return {"found": False, "pattern_id": pattern_id}

    all_series = _load("series.json")
    key = f"{record['backtest_run_id']}::{series}"
    if key not in all_series:
        available = sorted(
            k.split("::")[1]
            for k in all_series
            if k.startswith(record["backtest_run_id"] + "::")
        )
        return {
            "found": False,
            "pattern_id": record["pattern_id"],
            "series": series,
            "available_series": available,
        }

    entry = all_series[key]
    limit = max(1, min(limit, 200))
    rows = entry["rows"][offset : offset + limit]
    return {
        "found": True,
        "pattern_id": record["pattern_id"],
        "series": entry["series"],
        "description": entry["description"],
        "columns": entry["columns"],
        "row_count": entry["row_count"],
        "source_row_count": entry["source_row_count"],
        "downsampled": entry["downsampled"],
        "offset": offset,
        "returned": len(rows),
        "rows": rows,
    }


if __name__ == "__main__":
    import io
    import zipfile

    import uvicorn
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route
    from starlette.staticfiles import StaticFiles

    # Serve the stored report artifacts (charts, summaries) alongside the tool
    # endpoint, so `chart_url` resolves to something a browser on this machine
    # can actually open. Spec §5 puts these in object storage; this is the POC
    # stand-in for that, and deliberately loopback-only.
    app = mcp.streamable_http_app()
    runs = Path(__file__).resolve().parent.parent.parent / "trading" / "runs"

    if runs.is_dir():
        app.mount("/artifacts", StaticFiles(directory=runs), name="artifacts")

        async def bundle(request):
            """Zip a whole run directory on request.

            Built in memory and thrown away: these are small, and a cache is a
            staleness bug waiting to happen when a run is regenerated.
            """
            run_id = request.path_params["run_id"]
            # The run id comes from a URL. Resolve it and confirm it stays
            # inside runs/ before reading anything — otherwise ".." walks the
            # filesystem, and this process can read the user's home.
            target = (runs / run_id).resolve()
            if not target.is_dir() or runs.resolve() not in target.parents:
                return PlainTextResponse("no such run", status_code=404)

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(target.iterdir()):
                    if f.is_file():
                        z.write(f, arcname=f"{run_id}/{f.name}")
            buf.seek(0)
            return Response(
                buf.getvalue(),
                media_type="application/zip",
                headers={
                    "content-disposition": f'attachment; filename="{run_id}.zip"'
                },
            )

        app.router.routes.append(Route("/bundle/{run_id}.zip", bundle))
    else:
        print(f"warning: {runs} not found — artifact URLs will 404")

    uvicorn.run(app, host=HOST, port=PORT)
