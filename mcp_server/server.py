"""Read-only MCP tools for the trading assistant POC.

`backtests.*` reads the backtester's run directory live (see runs.py), so a
new backtest is visible to the assistant the moment it finishes. It previously
served a generated snapshot, which went stale the moment anything was re-run —
wrong for a system whose whole claim is knowing what has been tested.

`mt5.*` reads a live MT5 terminal through mt5_bridge/ — a read-only facade that
imports no trading function at all. There is no fixture fallback: an
unreachable terminal reports DISCONNECTED rather than serving a plausible fake.

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
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import mt5_live
import runs
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

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

_BT_ROOT = Path(__file__).resolve().parent.parent.parent / "trading"
_BT_PY = _BT_ROOT / ".venv" / "bin" / "python"
_BT_RUNNER = _BT_ROOT / "scripts" / "run_backtest.py"

HOST = "127.0.0.1"   # never bind wider: these tools expose account data
PORT = 8081

# Where a human opens a report. Served by apps/reports/server.py — a separate
# process with no tools in it, which is what makes it safe to expose while this
# one stays on loopback. Defaults to loopback so a workstation is honest about
# links only working locally; the server sets it to its own address.
REPORTS_URL = os.environ.get("PUBLIC_REPORTS_URL", "http://127.0.0.1:8083").rstrip("/")
REPORTS_PUBLIC = not REPORTS_URL.startswith(("http://127.0.0.1", "http://localhost"))

mcp = MCPServer("trading")

# Declared on every tool. MCP carries read-only as a machine-readable hint, so
# the guarantee travels with the tool definition rather than living only in a
# comment — a client can refuse anything not marked read-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def _bundle_bytes(run_id: str) -> bytes | None:
    """Zip one run directory in memory.

    The run id can arrive from a URL, so it is resolved and confirmed to sit
    inside runs/ before anything is read — otherwise ".." walks the filesystem.
    """
    import io
    import zipfile

    base = runs.run_root_for(run_id)
    if base is None:
        return None
    root = base.resolve()
    target = (base / run_id).resolve()
    if not target.is_dir() or root not in target.parents:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(target.iterdir()):
            if f.is_file():
                z.write(f, arcname=f"{run_id}/{f.name}")
    return buf.getvalue()


def _series_for(run_id: str) -> list[str]:
    """Which series exist for a run. Names only — the rows themselves are read
    on demand, since one equity curve is 100k rows."""
    return runs.series_names(run_id)


# ─────────────────────────────── mt5.* ────────────────────────────────────
# Normalized account state. Broker-specific shapes get flattened here so the
# agent never sees an MT5 quirk (spec §16).

@mcp.tool(name="mt5.get_account", annotations=READ_ONLY)
@_traced("mt5.get_account")
def mt5_get_account(credential_ref: str | None = None) -> dict:
    """Current state of the connected MT5 account: balance, equity, margin,
    open position count and connection health. Read-only.

    `credential_ref` is supplied by the server from the caller's connected
    account. You do not choose it, and a value you pass is discarded.
    `connection_state: NO_ACCOUNT` means this trader has no account connected —
    say that, rather than reporting an empty or absent account.

    `data_source` is "live" when the terminal answered. A payload with
    `connection_state: DISCONNECTED` means the terminal is unreachable —
    report that, rather than treating it as an empty account. There is no stub
    or demo mode; account data is live or it is absent.

    If `access` is "MASTER_TRADING_ENABLED" the account was connected with a
    trading-capable password instead of an investor one. Say so plainly: it is
    a security problem the trader needs to fix, not a detail to skip over.
    """
    return mt5_live.get_account(credential_ref)


@mcp.tool(name="mt5.get_positions", annotations=READ_ONLY)
@_traced("mt5.get_positions")
def mt5_get_positions(credential_ref: str | None = None) -> list | dict:
    """Currently open positions. Returns an empty list when flat — an empty
    result is a real answer, not a failure.

    A dict with `connection_state: DISCONNECTED` means the terminal is
    unreachable. That is not the same as being flat, and must never be reported
    as "no open positions".
    """
    return mt5_live.get_positions(credential_ref)


@mcp.tool(name="mt5.get_trade_history", annotations=READ_ONLY)
@_traced("mt5.get_trade_history")
def mt5_get_trade_history(limit: int = 20, symbol: str | None = None,
                          days: int = 90,
                          credential_ref: str | None = None) -> list | dict:
    """Closed trades, most recent last.

    One row per completed round trip, not per MT5 deal — entry and exit deals
    are paired by position, so nothing is double-counted. Positions still open
    are not here; use mt5.get_positions.

    Args:
        limit: maximum number of trades to return.
        symbol: optional instrument filter, e.g. "XAUUSD".
        days: how far back to search the account history.
    """
    return mt5_live.get_trade_history(limit=limit, symbol=symbol, days=days,
                                      credential_ref=credential_ref)


@mcp.tool(name="mt5.get_connection_status", annotations=READ_ONLY)
@_traced("mt5.get_connection_status")
def mt5_get_connection_status(credential_ref: str | None = None) -> dict:
    """Whether the MT5 terminal is reachable. Use this when a data call comes
    back DISCONNECTED, to tell a dropped connection from a quiet account."""
    return mt5_live.status(credential_ref)


# ──────────────────────────── backtests.* ─────────────────────────────────
# Retrieval only. There is deliberately no tool to run, queue or refresh a
# backtest — spec §33.

@mcp.tool(name="backtests.search", annotations=READ_ONLY)
@_traced("backtests.search")
def backtests_search(
    instrument: str | None = None,
    timeframe: str | None = None,
) -> dict:
    """Find stored backtests and studies, newest version of each.

    Returns {"count": N, "total_stored": M, "patterns": [...]}. **Report all
    N.** If you name fewer than `count`, you are telling the trader we have
    less evidence than we do — the most expensive kind of wrong answer this
    system can give.

    **Pass a filter only when the trader named one.** "What do we have stored?"
    means everything; do not narrow it to an instrument they did not mention.
    When `count` is less than `total_stored` you are looking at a subset, and
    calling it "everything we have" is wrong — say what you filtered by, or
    search again without the filter.

    Rows carry `validated`: true means human-reviewed, false means an
    exploratory run. Say which is which rather than omitting the exploratory
    ones.

    A row with `win_rate: null` is a structural study, not a missing value. It
    has no entries or P&L so it cannot have a win rate, and it is still a
    stored result worth reporting.

    An empty list means we have no evidence for that scenario, and never that
    it should be estimated.

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
    patterns = [
        {
            "pattern_id": r["pattern_id"],
            "version": r["version"],
            "versions_available": runs.version_count(r["pattern_id"]),
            "validated": r["validated"],
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
    total = len(runs.latest_versions())
    applied = {k: v for k, v in
               (("instrument", instrument), ("timeframe", timeframe)) if v}
    out = {
        "count": len(patterns),
        # A filtered search otherwise looks identical to an exhaustive one, and
        # gets reported as "everything we have". Carrying the total makes the
        # narrowing visible in the same payload.
        "total_stored": total,
        "filter_applied": applied or None,
        "validated_count": sum(1 for p in patterns if p["validated"]),
        "exploratory_count": sum(1 for p in patterns if not p["validated"]),
        "patterns": patterns,
    }
    if applied and len(patterns) < total:
        out["note"] = (
            f"Filtered by {applied}. {total - len(patterns)} other stored "
            f"pattern(s) did not match — this is not everything we have."
        )
    return out


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
    root = runs.run_root_for(run_id) or runs.RUNS_DIR
    files = sorted(f.name for f in (root / run_id).iterdir() if f.is_file())
    return {
        "found": True,
        "pattern_id": r["pattern_id"],
        "version": r["version"],
        "versions_available": runs.version_count(r["pattern_id"]),
        "backtest_run_id": run_id,
        "files": files,
        "artifacts": {
            "report_url": f"{REPORTS_URL}/r/{run_id}/",
            "chart_url": f"{REPORTS_URL}/r/{run_id}/chart.html",
            "summary_url": f"{REPORTS_URL}/r/{run_id}/summary.json",
            "bundle_url": f"http://{HOST}:{PORT}/bundle/{run_id}.zip",
        },
        "artifact_note": (
            "report_url opens the run's chart in a browser and is the thing to "
            "hand someone who asks to see results — a link they can tap beats a "
            "zip they have to unpack. "
            + ("It is reachable from anywhere, including a phone. "
               if REPORTS_PUBLIC else
               "It only resolves on the machine running this assistant, so say "
               "so rather than implying it works from a phone. ")
            + "bundle_url is a zip of every file in the run; offer it when "
            "someone wants the files themselves, and note it is loopback-only. "
            "backtests.send_report is what actually delivers a zip to Telegram. "
            "You cannot attach files yourself — never imply you attached anything."
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


# ─────────────────────── backtests.run — TEMPORARY ────────────────────────
# Spec §33 says the assistant must never initiate a backtest, and §9.2 forbids
# generating statistics. This tool deliberately breaks both, on an explicit
# decision recorded in docs/BACKTEST_EXECUTION.md, for the POC only. The spec
# is unchanged and still describes the intended product.
#
# The containment that makes it reversible: ad-hoc runs are written to
# runs-adhoc/, indexed separately, and every record carries validated:false.
# A number produced four seconds ago to answer a question never acquires the
# standing of one that was designed, reviewed and published.

@mcp.tool(
    name="backtests.list_strategies",
    annotations=READ_ONLY,
)
@_traced("backtests.list_strategies")
def backtests_list_strategies() -> dict:
    """Strategies the backtester can run, with their parameters and defaults.

    Call this before backtests.run rather than guessing a parameter name.
    """
    proc = subprocess.run(
        [str(_BT_PY), str(_BT_RUNNER), "--list-strategies"],
        cwd=str(_BT_ROOT), capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return {"error": "could not list strategies", "detail": proc.stderr[-400:]}
    return {"strategies": proc.stdout.strip()}


@mcp.tool(
    name="backtests.fetch_data",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
@_traced("backtests.fetch_data")
def backtests_fetch_data(
    symbol: str,
    timeframes: str = "M15,H1,D1",
    start: str | None = None,
) -> dict:
    """Download bars from the live MT5 terminal so a backtest has data to run on.

    Call this when `backtests.run` fails with "No <symbol> <tf> bars" — that is
    the only reason to call it. It is slow (minutes for years of M15), it talks
    to the broker, and it writes to the bar store every backtest reads, so it
    is not something to do speculatively or to "refresh" data that is already
    there.

    Say you are fetching before you start, because the trader will wait.

    Two steps, both here so the store cannot be left half-populated: download
    to the CSV staging area, then convert into the Parquet store the backtester
    reads.

    Args:
        symbol: e.g. "GBPUSD". Must exist on the broker's symbol list.
        timeframes: comma-separated, e.g. "M15,H1,D1".
        start: first bar, "YYYY-MM-DD". Defaults to the fetcher's own default.
    """
    # Fail in seconds rather than thirty minutes. The fetcher launches the
    # terminal itself when none is running, and mt5.initialize() hangs
    # indefinitely in exactly that case — the terminal starts, the client never
    # completes its handshake with the instance it spawned. A host without a
    # warm terminal is a configuration problem to report, not something to sit
    # in a subprocess timeout for.
    # One terminal serves the bridge and this fetch. deploy/trading-signals.service
    # takes the same lock for the same reason: without it the two intermittently
    # kill each other's connection. fetch-mt5.sh does not take it itself.
    # -w 30, not the 300 that deploy/trading-signals.service uses. That is a
    # nightly batch job where waiting five minutes for the terminal costs
    # nothing; here someone is watching a chat. If the terminal is busy that
    # long, saying so beats making them wait.
    lock = ["/usr/bin/flock", "-w", "30", "/tmp/mt5.lock"]

    fetch = lock + [str(_BT_ROOT / "scripts" / "fetch-mt5.sh"),
                    "--symbol", symbol, "--timeframe", timeframes,
                    "--out", "csv://data/incoming", "--dump-spec"]
    if start:
        fetch += ["--start", start]
    try:
        p1 = subprocess.run(fetch, cwd=str(_BT_ROOT), capture_output=True,
                            text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "fetch timed out after 5 minutes",
                "detail": "usually means the symbol is not on the broker's list, "
                          "or no terminal could be reached; on a headless host "
                          "check mt5-terminal.service is running"}
    if p1.returncode != 0:
        return {"ok": False, "error": "fetch failed", "symbol": symbol,
                "detail": ((p1.stderr or p1.stdout) or "")[-600:]}

    convert = [str(_BT_PY), str(_BT_ROOT / "scripts" / "manage_data.py"), "convert",
               "--from", "csv://data/incoming", "--to", "parquet://data/bars"]
    try:
        p2 = subprocess.run(convert, cwd=str(_BT_ROOT), capture_output=True,
                            text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "convert timed out",
                "detail": "bars were downloaded but not loaded into the store"}
    if p2.returncode != 0:
        return {"ok": False, "error": "convert failed",
                "detail": ((p2.stderr or p2.stdout) or "")[-600:],
                "note": "bars were downloaded but are not queryable yet"}

    out = p1.stdout or ""
    stored = [l.strip() for l in out.splitlines() if "stored" in l and "bars" in l]
    # The fetcher warns when the terminal's tick_value disagrees with the
    # contract spec. That scales absolute P&L, so it must not be swallowed —
    # a wrong dollar figure presented confidently is the failure this whole
    # service is built to avoid.
    warnings = [l.strip().lstrip("! ") for l in out.splitlines()
                if l.strip().startswith("!")]

    return {
        "ok": True,
        "symbol": symbol,
        "timeframes": timeframes,
        "stored": stored,
        "warnings": warnings,
        "note": ("Bars are in the store; backtests.run will now find them. "
                 + ("Report these warnings when you quote absolute P&L from a "
                    "backtest on this data. " if warnings else "")
                 + "This data came from the broker just now and has had no "
                   "review — it does not make a run on it validated evidence."),
    }


@mcp.tool(
    name="backtests.run",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
@_traced("backtests.run")
def backtests_run(
    strategy: str,
    symbol: str,
    timeframe: str,
    params: dict | None = None,
    start: str | None = None,
    end: str | None = None,
    intrabar: str = "conservative",
) -> dict:
    """Run a NEW backtest and return its metrics.

    The result is **exploratory, not validated evidence**. It has had no review
    and, unless you passed `end`, no out-of-sample split — the whole period is
    in-sample. Present it as "I just ran this", never alongside the validated
    patterns as though it carried the same weight, and never as Level A.

    Two things to say out loud when reporting a result:

    - it is unvalidated and freshly computed;
    - a parameter the trader chose after seeing earlier results is fitted to
      those results. If they ask you to sweep values until something looks
      good, say what that does to the number rather than just running it.

    Args:
        strategy: from backtests.list_strategies, e.g. "day_open", "sma_cross".
        symbol: e.g. "XAUUSD".
        timeframe: e.g. "M15", "H4".
        params: strategy parameters, e.g. {"stop_pct": 2.0}.
        start: first bar, "YYYY-MM-DD".
        end: last bar. Setting this is what holds later data out of sample.
        intrabar: conservative | optimistic | ohlc.
    """
    argv = [
        str(_BT_PY), str(_BT_RUNNER),
        "--strategy", strategy, "--symbol", symbol, "--timeframe", timeframe,
        "--intrabar", intrabar,
        "--save", "--runs-dir", "runs-adhoc", "--label", "adhoc",
        "--json", "--quiet",
    ]
    for k, v in (params or {}).items():
        argv += ["--param", f"{k}={v}"]
    if start:
        argv += ["--start", start]
    if end:
        argv += ["--end", end]

    try:
        proc = subprocess.run(argv, cwd=str(_BT_ROOT), capture_output=True,
                              text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "backtest timed out after 600s"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout)[-600:]
        # The loader's message ends with a shell command the model cannot run.
        # Point it at the tool that does the same thing, or it will either give
        # up or claim it ran something it did not.
        out = {"ok": False, "error": "backtest failed", "detail": detail}
        if "No " in detail and "bars in" in detail:
            out["error"] = "no bars for that symbol and timeframe"
            out["fix"] = (f"call backtests.fetch_data(symbol=\"{symbol}\", "
                          f"timeframes=\"{timeframe}\") first, then run again")
        return out

    metrics, run_id = None, None
    try:
        text = proc.stdout
        metrics = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        pass
    for line in proc.stdout.splitlines():
        if "Saved to" in line:
            run_id = line.split("runs-adhoc/")[-1].strip()

    # Provenance belongs in the run, not in its file path. summary.json is
    # byte-identical whether a human designed the run or a chat message
    # triggered it, so without this there is nothing in the artifact that says
    # which — only where it happens to sit.
    if run_id:
        try:
            (runs.ADHOC_DIR / run_id / "provenance.json").write_text(json.dumps({
                "initiated_by": "assistant",
                "initiated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reviewed": False,
                "out_of_sample": bool(end),
                "requested": {
                    "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
                    "params": params or {}, "start": start, "end": end,
                    "intrabar": intrabar,
                },
            }, indent=2) + "\n")
        except OSError:
            pass

    return {
        "ok": True,
        "validated": False,
        "evidence_level": "exploratory — freshly computed, unreviewed",
        "out_of_sample": bool(end),
        "run_id": run_id,
        "pattern_id": runs.pattern_id_for(run_id) if run_id else None,
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
        "params": params or {},
        "metrics": metrics,
        "caveat": (
            "Not validated evidence. No review, and no out-of-sample split "
            "unless `end` was set. Report it as a run you just did."
        ),
    }


@mcp.tool(
    name="backtests.run_zone_study",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
@_traced("backtests.run_zone_study")
def backtests_run_zone_study(
    symbol: str,
    timeframe: str = "H4",
    contract: str | None = None,
    deviation_pct: float = 2.0,
    initial_ratio: float = 1.1,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Run a NEW margin-zone study — ZigZag pivots, margin envelopes, rollover
    crossings.

    This is a **different kind of run from backtests.run**, and the difference
    matters when you report it. A zone study has no entries, no exits and no
    P&L: it measures how often price reached a level, not whether trading
    toward it made money. It therefore has no win rate and no expectancy, and
    you must not describe reach rates as though they were either.

    Like backtests.run, the output is exploratory and unreviewed.

    Args:
        symbol: e.g. "EURUSD".
        timeframe: bar size for the pivots, e.g. "H4".
        contract: CME contract supplying the margin, e.g. "6E". Required for
            the margin envelopes; without it only pivots are meaningful.
        deviation_pct: ZigZag reversal threshold as a percentage of price.
        initial_ratio: initial margin ratio for the first zone.
        start: first bar, "YYYY-MM-DD".
        end: last bar.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"zones_{symbol}_{timeframe}" + (f"_{contract}" if contract else "")
    run_id = f"{stamp}_{tag}_dev{deviation_pct}pct_adhoc".replace(".", "")
    out = runs.ADHOC_DIR / run_id

    argv = [str(_BT_PY), str(_BT_ROOT / "scripts" / "plot_zones.py"),
            "--symbol", symbol, "--timeframe", timeframe,
            "--deviation-pct", str(deviation_pct),
            "--initial-ratio", str(initial_ratio),
            "--save", "--out", str(out)]
    if contract:
        argv += ["--contract", contract]
    if start:
        argv += ["--start", start]
    if end:
        argv += ["--end", end]

    try:
        proc = subprocess.run(argv, cwd=str(_BT_ROOT), capture_output=True,
                              text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "zone study timed out after 900s"}
    if proc.returncode != 0:
        return {"ok": False, "error": "zone study failed",
                "detail": (proc.stderr or proc.stdout)[-600:]}

    summary = None
    try:
        summary = json.loads((out / "summary.json").read_text())
    except (OSError, ValueError):
        pass

    try:
        (out / "provenance.json").write_text(json.dumps({
            "initiated_by": "assistant",
            "initiated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewed": False,
            "out_of_sample": bool(end),
            "requested": {
                "kind": "zone_study", "symbol": symbol, "timeframe": timeframe,
                "contract": contract, "deviation_pct": deviation_pct,
                "initial_ratio": initial_ratio, "start": start, "end": end,
            },
        }, indent=2) + "\n")
    except OSError:
        pass

    return {
        "ok": True,
        "kind": "study",
        "validated": False,
        "evidence_level": "exploratory — freshly computed, unreviewed",
        "run_id": run_id,
        "pattern_id": runs.pattern_id_for(run_id),
        "summary": summary,
        "caveat": (
            "A structural study: no entries, exits or P&L. Reach rates are not "
            "win rates and say nothing about whether trading toward these "
            "zones was profitable. Unreviewed."
        ),
        "console": proc.stdout.strip()[-800:],
    }


@mcp.tool(
    name="backtests.send_report",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
@_traced("backtests.send_report")
def backtests_send_report(pattern_id: str, version: int | None = None,
                          chat_id: str | None = None) -> dict:
    """Deliver a pattern's full run bundle to the trader as a Telegram file.

    Use this when someone asks for the files, a zip, or the report itself. The
    artifact URLs from `backtests.get_report` only resolve on the machine
    running this assistant, so they are useless to someone reading on a phone —
    this actually sends the file.

    Returns {"sent": true} on success. On failure say what failed; do not claim
    a file was sent.

    `chat_id` is filled in by the server from the caller's own Telegram
    binding. You do not choose it, and a value you pass is discarded.
    """
    r = runs.find(pattern_id, version)
    if r is None:
        return {"sent": False, "error": f"no such pattern: {pattern_id}"}

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    # No default destination. A fallback chat would turn a missing-scope bug
    # into a file delivered to the wrong person — the same failure shape the
    # bridge refuses when a request arrives without a ref.
    if not chat_id:
        return {"sent": False,
                "error": "no destination chat",
                "detail": "chat_id is supplied by the caller's Telegram binding; "
                          "there is no default recipient"}
    if not token:
        return {"sent": False, "error": "TELEGRAM_BOT_TOKEN is not set"}

    run_id = r["backtest_run_id"]
    blob = _bundle_bytes(run_id)
    if blob is None:
        return {"sent": False, "error": f"run directory missing: {run_id}"}
    if len(blob) > 45 * 1024 * 1024:        # Telegram caps bot uploads at 50MB
        return {"sent": False, "error": "bundle too large to send",
                "size_mb": round(len(blob) / 1048576, 1)}

    caption = (f"{r['pattern_id']} v{r['version']} — {r['instrument']} "
               f"{r['timeframe']}, run {run_id}")
    ok, detail = _telegram_send_document(token, chat_id, f"{run_id}.zip", blob, caption)
    return ({"sent": True, "pattern_id": r["pattern_id"], "version": r["version"],
             "filename": f"{run_id}.zip", "size_bytes": len(blob)}
            if ok else {"sent": False, "error": "telegram rejected the upload",
                        "detail": detail})


def _telegram_send_document(token: str, chat_id: str, filename: str,
                            blob: bytes, caption: str) -> tuple[bool, str]:
    """POST one multipart sendDocument. Stdlib only — this runs in a process
    that has no business growing an HTTP dependency."""
    import urllib.request

    boundary = "----mcp" + uuid.uuid4().hex
    def part(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()

    body = b"".join([
        part("chat_id", chat_id),
        part("caption", caption),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
         f"filename=\"{filename}\"\r\nContent-Type: application/zip\r\n\r\n").encode(),
        blob,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
            return bool(payload.get("ok")), json.dumps(payload)[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    import io
    import zipfile

    import uvicorn
    from starlette.responses import FileResponse, PlainTextResponse, Response
    from starlette.routing import Route

    # Serve the stored report artifacts (charts, summaries) alongside the tool
    # endpoint, so `chart_url` resolves to something a browser on this machine
    # can actually open. Spec §5 puts these in object storage; this is the POC
    # stand-in for that, and deliberately loopback-only.
    app = mcp.streamable_http_app()
    # Do not bind a local named `runs` in this block: that name is the
    # imported module, and shadowing it turns every backtests.* tool into an
    # AttributeError at request time. Only the real server executes this
    # block, so in-process tests cannot see that class of fault.
    def _resolve(run_id: str, rel: str | None = None):
        """Locate a run's directory, or a file inside it, across both roots.

        Paths arrive from URLs, so the result is resolved and confirmed to sit
        under the root that owns the run before anything is read — otherwise
        ".." walks out and this process serves the user's home directory.
        """
        base = runs.run_root_for(run_id)
        if base is None:
            return None
        root = base.resolve()
        target = (base / run_id).resolve()
        if not target.is_dir() or root not in target.parents:
            return None
        if rel is None:
            return target
        f = (target / rel).resolve()
        if not f.is_file() or target not in f.parents:
            return None
        return f

    async def artifact(request):
        f = _resolve(request.path_params["run_id"], request.path_params["path"])
        if f is None:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(f)

    async def bundle(request):
        """Zip a whole run directory on request.

        Built in memory and thrown away: these are small, and a cache is a
        staleness bug waiting to happen when a run is regenerated.
        """
        run_id = request.path_params["run_id"]
        blob = _bundle_bytes(run_id)
        if blob is None:
            return PlainTextResponse("no such run", status_code=404)
        return Response(
            blob,
            media_type="application/zip",
            headers={"content-disposition": f'attachment; filename="{run_id}.zip"'},
        )

    if runs.RUNS_DIR.is_dir() or runs.ADHOC_DIR.is_dir():
        # Served through one URL space regardless of which directory holds the
        # run. Two URL shapes would leak an internal filing decision into
        # links people paste to each other.
        app.router.routes.append(Route("/artifacts/{run_id}/{path:path}", artifact))
        app.router.routes.append(Route("/bundle/{run_id}.zip", bundle))
    else:
        print(f"warning: no run directories found — artifact URLs will 404")

    uvicorn.run(app, host=HOST, port=PORT)
