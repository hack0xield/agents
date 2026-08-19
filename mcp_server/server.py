"""Read-only MCP tools for the trading assistant POC.

`backtests.*` reads the backtester's run directory live (see runs.py), so a
new backtest is visible to the assistant the moment it finishes. It previously
served a generated snapshot, which went stale the moment anything was re-run —
wrong for a system whose whole claim is knowing what has been tested.

`mt5.*` is still fixture-backed. There is no live MT5 connection in the POC and
nothing in runs/ describes an account, so that data has nowhere real to come
from yet. It is stub data and TOOLS.md requires the agent to say so.

Two guarantees this module exists to enforce, both from spec §10:

  1. There is no tool that can place, modify or close a trade, and none will be
     added. Read-only is a product guarantee backed by the MT5 investor
     password, not a limitation of the stub.

  2. `backtests.*` retrieves stored results. It never runs a backtest (§33).

Run:  .venv/bin/python mcp_server/server.py
"""

from __future__ import annotations

import functools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runs
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALL_LOG = Path(__file__).resolve().parent / "tool-calls.jsonl"


def _log_call(tool: str, args: dict, result_summary: str) -> None:
    """Append one line per tool call.

    OpenClaw's trajectory export records zero tool events when running through
    the claude-cli provider: that provider drives the tool loop itself, so the
    gateway never observes the individual calls. Spec §53 wants tools_called in
    the audit trail, and this is the only vantage point that currently sees
    them — the tool server itself.

    Deliberately not a substitute for the real audit trail, which also needs
    the run id, user id, model, tokens and cost. Those live on the agent side.
    """
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "args": {k: v for k, v in args.items() if v is not None},
        "result": result_summary,
    }
    line = json.dumps(rec)
    try:
        with CALL_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # a log write must never break a tool call
    print(f"[tool] {line}", file=sys.stderr, flush=True)


def _traced(tool: str):
    """Wrap a tool so every invocation is logged with a size-bounded summary."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            out = fn(*a, **kw)
            if isinstance(out, list):
                summary = f"{len(out)} row(s)"
            elif isinstance(out, dict):
                if out.get("found") is False:
                    summary = "not found"
                elif "returned" in out:
                    summary = f"{out['returned']} of {out.get('row_count')} row(s)"
                else:
                    summary = f"{len(out)} field(s)"
            else:
                summary = type(out).__name__
            _log_call(tool, kw, summary)
            return out

        return wrapper

    return deco

HOST = "127.0.0.1"   # never bind wider: these tools expose account data
PORT = 8081

mcp = MCPServer("trading")

# Declared on every tool. MCP carries read-only as a machine-readable hint, so
# the guarantee travels with the tool definition rather than living only in a
# comment — a client can refuse anything not marked read-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _series_for(run_id: str) -> list[str]:
    """Which series exist for a run. Names only — the rows themselves are read
    on demand, since one equity curve is 100k rows."""
    return runs.series_names(run_id)


# ─────────────────────────────── mt5.* ────────────────────────────────────
# Normalized account state. Broker-specific shapes get flattened here so the
# agent never sees an MT5 quirk (spec §16).

@mcp.tool(name="mt5.get_account", annotations=READ_ONLY)
@_traced("mt5.get_account")
def mt5_get_account() -> dict:
    """Current state of the connected MT5 account: balance, equity, margin,
    open position count and connection health. Read-only."""
    return _load("account.json")


@mcp.tool(name="mt5.get_positions", annotations=READ_ONLY)
@_traced("mt5.get_positions")
def mt5_get_positions() -> list[dict]:
    """Currently open positions. Returns an empty list when flat — an empty
    result is a real answer, not a failure."""
    return _load("positions.json")


@mcp.tool(name="mt5.get_trade_history", annotations=READ_ONLY)
@_traced("mt5.get_trade_history")
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
@_traced("backtests.search")
def backtests_search(
    instrument: str | None = None,
    timeframe: str | None = None,
) -> list[dict]:
    """Find validated backtests matching an instrument and/or timeframe.

    Returns the newest version of each pattern. Returns an empty list when
    nothing matches — that means we have no validated evidence for the
    scenario, and never that it should be estimated.

    Args:
        instrument: e.g. "XAUUSD". Case-insensitive.
        timeframe: e.g. "H4", "M15". Case-insensitive.
    """
    rows = runs.latest_versions()
    if instrument:
        rows = [r for r in rows if (r["instrument"] or "").upper() == instrument.upper()]
    if timeframe:
        rows = [r for r in rows if (r["timeframe"] or "").upper() == timeframe.upper()]
    # Headline fields only. The full record is several KB per pattern and most
    # questions are answered without it (spec §41).
    return [
        {
            "pattern_id": r["pattern_id"],
            "version": r["version"],
            "versions_available": runs.version_count(r["pattern_id"]),
            "kind": r["kind"],
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
@_traced("backtests.get_summary")
def backtests_get_summary(pattern_id: str, version: int | None = None) -> dict:
    """Full stored record for one pattern: metrics, conditions, invalidations,
    execution assumptions and stated limitations.

    Omit `version` for the newest. Read `limitations` before relying on any
    number here. Returns {"found": false} if the pattern is not in the database.
    """
    r = runs.find(pattern_id, version)
    if r is None:
        return {"found": False, "pattern_id": pattern_id, "version": version}
    return {**r, "found": True, "versions_available": runs.version_count(r["pattern_id"])}


@mcp.tool(name="backtests.get_report", annotations=READ_ONLY)
@_traced("backtests.get_report")
def backtests_get_report(pattern_id: str, version: int | None = None) -> dict:
    """Locate the stored artifacts for a pattern version.

    Retrieves what already exists. It never initiates a new backtest (§33).
    """
    r = runs.find(pattern_id, version)
    if r is None:
        return {"found": False, "pattern_id": pattern_id, "version": version}
    run_id = r["backtest_run_id"]
    files = sorted(f.name for f in (runs.RUNS_DIR / run_id).iterdir() if f.is_file())
    return {
        "found": True,
        "pattern_id": r["pattern_id"],
        "version": r["version"],
        "versions_available": runs.version_count(r["pattern_id"]),
        "backtest_run_id": run_id,
        "files": files,
        "artifacts": {
            "chart_url": f"http://{HOST}:{PORT}/artifacts/{run_id}/chart.html",
            "summary_url": f"http://{HOST}:{PORT}/artifacts/{run_id}/summary.json",
            "bundle_url": f"http://{HOST}:{PORT}/bundle/{run_id}.zip",
        },
        "artifact_note": (
            "bundle_url is a zip of every file in the run. Offer it when someone "
            "asks for the files themselves. These URLs are served from the "
            "machine running this assistant: they open in a browser there, and "
            "are not reachable from a phone or another host. You cannot attach "
            "or send files, so hand over the link and say where it works — never "
            "imply you attached anything."
        ),
        "available_series": _series_for(run_id),
        "limitations": r.get("limitations", []),
    }


@mcp.tool(name="backtests.get_series", annotations=READ_ONLY)
@_traced("backtests.get_series")
def backtests_get_series(
    pattern_id: str,
    series: str,
    limit: int = 50,
    offset: int = 0,
    version: int | None = None,
) -> dict:
    """Underlying data behind a pattern's charts — the numbers a plot is drawn
    from, not the plot image.

    Call `backtests.get_report` first to see which series a pattern has.

    Returns rows plus `row_count` and `returned`, so you can tell a page from
    the whole series. Never claim a total from a page: if `returned` is less
    than `row_count`, you are looking at a slice.

    Args:
        pattern_id: e.g. "ZONES_EURUSD_H4_6E_DEV2PCT".
        series: e.g. "envelopes", "pivots", "crossings", "rollover".
        limit: rows to return, capped at 200 — a model reasoning over hundreds
            of raw rows is expensive and rarely more accurate than reasoning
            over the summary.
        offset: rows to skip, for paging through a longer series.
        version: pattern version; omit for the newest.
    """
    r = runs.find(pattern_id, version)
    if r is None:
        return {"found": False, "pattern_id": pattern_id}

    run_id = r["backtest_run_id"]
    entry = runs.read_series(run_id, series)
    if entry is None:
        return {
            "found": False,
            "pattern_id": r["pattern_id"],
            "series": series,
            "available_series": runs.series_names(run_id),
        }

    limit = max(1, min(limit, 200))
    rows = entry["rows"][offset : offset + limit]
    return {
        "found": True,
        "pattern_id": r["pattern_id"],
        "version": r["version"],
        "backtest_run_id": run_id,
        "series": entry["series"],
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
    # NOT `runs`: that name is the imported module, and rebinding it here
    # silently turns every backtests.* tool into an AttributeError at
    # request time. Only the real server executes this block, so the
    # in-process tests could not see it.
    runs_dir = runs.RUNS_DIR

    if runs_dir.is_dir():
        app.mount("/artifacts", StaticFiles(directory=runs_dir), name="artifacts")

        async def bundle(request):
            """Zip a whole run directory on request.

            Built in memory and thrown away: these are small, and a cache is a
            staleness bug waiting to happen when a run is regenerated.
            """
            run_id = request.path_params["run_id"]
            # The run id comes from a URL. Resolve it and confirm it stays
            # inside runs/ before reading anything — otherwise ".." walks the
            # filesystem, and this process can read the user's home.
            target = (runs_dir / run_id).resolve()
            if not target.is_dir() or runs_dir.resolve() not in target.parents:
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
        print(f"warning: {runs_dir} not found — artifact URLs will 404")

    uvicorn.run(app, host=HOST, port=PORT)
