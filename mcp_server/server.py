"""MCP tools for the trading assistant POC.

`backtests.*` reads the backtester's run directory live (see runs.py), so a
new backtest is visible to the assistant the moment it finishes. It previously
served a generated snapshot, which went stale the moment anything was re-run —
wrong for a system whose whole claim is knowing what has been tested.

`mt5.*` reads a live MT5 terminal through mt5_bridge/ — a read-only facade that
imports no trading function at all. There is no fixture fallback: an
unreachable terminal reports DISCONNECTED rather than serving a plausible fake.

`live.*` starts, stops and reports on the live trader: a strategy trading the
shared test account by its own rules (see live_sessions.py).

The guarantee this module exists to enforce, from spec §10: there is no tool
that can place, modify or close a trade, and none will be added. Read-only
access to a user's account is backed by the MT5 investor password.

Two POC deviations from the spec, each recorded with its reasons:

  1. `backtests.run` runs new backtests (§33) — docs/BACKTEST_EXECUTION.md.
  2. `live.start` and `live.stop` run a strategy that trades the shared test
     account ("the assistant does not execute trades") — docs/LIVE_TRADING.md.

Run:  .venv/bin/python mcp_server/server.py
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import live_sessions
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


_CONFIG_DIR = _BT_ROOT / "configs" / "strategies"


def _resolve_config(name: str) -> Path | None:
    """A stored run config, by name, refusing anything outside its directory.

    The name arrives from the model and is handed to a subprocess, so the
    directory part is dropped outright rather than sanitised: `Path(name).name`
    turns "../../.env" into ".env", which then fails the suffix and existence
    checks. Resolving and comparing the parent is the belt to that braces.
    """
    candidate = Path(name).name
    if not candidate.endswith((".yaml", ".yml", ".json")):
        return None
    path = (_CONFIG_DIR / candidate).resolve()
    if path.parent != _CONFIG_DIR.resolve() or not path.is_file():
        return None
    return path


def _list_configs() -> list[dict]:
    """Stored run configs, with the first comment line as a description.

    Read as text, not parsed: the description lives in a comment, which no
    YAML loader would hand back, and this avoids a PyYAML dependency in a venv
    that has never needed one.
    """
    out = []
    if not _CONFIG_DIR.is_dir():
        return out
    for f in sorted(_CONFIG_DIR.iterdir()):
        if f.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        description = ""
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("#"):
                    description = line.lstrip("# ").strip()
                    break
                if line.strip():
                    break
        except OSError:
            pass
        out.append({"config": f.name, "description": description})
    return out


def _config_values(path: Path) -> dict:
    """symbol / timeframe / start from a config, loaded by the code that owns it.

    Run through the backtester's own venv and loader rather than reimplemented
    here: it is the thing that decides what a config file means, and a second
    parser in this process would drift from it. Returns {} if anything fails —
    the caller only needs these to aim the bar refresh, and a refresh that is
    skipped is reported, not fatal.
    """
    script = (
        "import json,sys;sys.path.insert(0,%r);from backtester import cli;"
        "c=cli.load_config(%r);"
        "print(json.dumps({k:c.get(k) for k in "
        "('symbol','timeframe','start','end','strategy')}, default=str))"
        % (str(_BT_ROOT), str(path))
    )
    try:
        proc = subprocess.run([str(_BT_PY), "-c", script], cwd=str(_BT_ROOT),
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return json.loads(proc.stdout.strip() or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {}


def _report_urls(run_id: str) -> dict:
    """Where a run can be read in a browser, and whether that link travels.

    A fresh run is exactly when someone asks to see the chart, so the link
    belongs in the run's own result rather than behind a second call to
    get_report. Whether it resolves off this machine is a deployment fact
    (PUBLIC_REPORTS_URL, set by deploy/install.sh), never something to assert
    from memory — the note says which one is true here.
    """
    return {
        "report_url": f"{REPORTS_URL}/r/{run_id}/",
        "chart_url": f"{REPORTS_URL}/r/{run_id}/chart.html",
        "link_note": (
            "Reachable from anywhere, including a phone — hand it over."
            if REPORTS_PUBLIC else
            "Only resolves on the machine running this assistant, so say so "
            "rather than implying it opens on a phone."
        ),
    }


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
#
# Off unless MT5_ACCOUNT_TOOLS=on. The server has one MT5 terminal, shared by
# the bridge behind these tools and the live trader. Reading an account makes
# the bridge log that terminal in with the account's investor password, and on
# the shared account that leaves the live trader unable to trade
# (docs/LIVE_TRADING.md, "First days live"). Back on once the live trader has
# a terminal of its own.
ACCOUNT_TOOLS = os.environ.get("MT5_ACCOUNT_TOOLS", "off").strip().lower() == "on"


def _account_tool(name: str):
    """Register an mt5.* tool only while the account tools are on."""
    if not ACCOUNT_TOOLS:
        return lambda fn: fn
    return mcp.tool(name=name, annotations=READ_ONLY)


@_account_tool("mt5.get_account")
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


@_account_tool("mt5.get_positions")
@_traced("mt5.get_positions")
def mt5_get_positions(credential_ref: str | None = None) -> list | dict:
    """Currently open positions. Returns an empty list when flat — an empty
    result is a real answer, not a failure.

    A dict with `connection_state: DISCONNECTED` means the terminal is
    unreachable. That is not the same as being flat, and must never be reported
    as "no open positions".
    """
    return mt5_live.get_positions(credential_ref)


@_account_tool("mt5.get_trade_history")
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


@_account_tool("mt5.get_connection_status")
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

    `pattern_ids` lists every id in this result. Account for all of them —
    that list is the check on N above.

    Rows carry `validated`, recording whether a human reviewed the run. It is
    filing metadata, not a ranking: report reviewed and unreviewed rows the
    same way, and never omit the unreviewed ones. Quote `sample_size` with any
    rate you take from a row.

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
        # Not redundant with `patterns`. A flat id list is the thing an
        # omission shows up against: the model can quietly drop a row from a
        # list of objects, but a name it was handed and never mentioned is
        # visible. This replaces the validated/exploratory split counts, which
        # did the same job by making the model reconcile two numbers — and did
        # it by ranking the rows, which is exactly what we no longer want.
        "pattern_ids": [p["pattern_id"] for p in patterns],
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
    """Strategies the backtester can run, with their parameters and defaults,
    and the stored run configs that combine them with a period and execution
    costs.

    Call this before backtests.run rather than guessing a parameter name — or a
    config name, which `configs` lists. A config is the only way to reach the
    execution settings (starting balance, leverage, spread, commission): those
    are not parameters and cannot be passed through `params`.
    """
    proc = subprocess.run(
        [str(_BT_PY), str(_BT_RUNNER), "--list-strategies"],
        cwd=str(_BT_ROOT), capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return {"error": "could not list strategies", "detail": proc.stderr[-400:]}
    return {
        "strategies": proc.stdout.strip(),
        "configs": _list_configs(),
        "configs_note": (
            "Pass one of these as backtests.run(config=...) to run it exactly "
            "as written — its symbol, timeframe, period and execution costs "
            "included. Without a config, a run uses the engine defaults: "
            "10,000 balance, 100:1 leverage, and every bar in the store."
        ),
    }


def _refresh_bars(symbol: str, timeframe: str, start: str | None) -> dict:
    """Re-download the range a backtest is about to read.

    The whole range, not a computed gap. A full pull is seconds, and both bar
    stores merge and dedupe on write, so re-fetching is cheaper than the code
    needed to work out what was missing — and it cannot leave a hole at the
    seam the way an off-by-one in that arithmetic would.

    Never fatal. A backtest on slightly stale bars is still a real backtest;
    what is not acceptable is running on stale data and calling it current, so
    the outcome is always returned for the model to pass on.
    """
    r = backtests_fetch_data(symbol=symbol, timeframes=timeframe, start=start)
    if not r.get("ok"):
        return {"refreshed": False, "why": r.get("error"),
                "detail": r.get("detail"),
                "note": ("Bars were NOT refreshed, so this ran on whatever was "
                         "already stored. Say so when reporting the result "
                         "rather than presenting it as current.")}
    return {"refreshed": True, "stored": r.get("stored") or [],
            "warnings": r.get("warnings") or []}


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
    # One terminal serves the bridge and this fetch; without the lock the two
    # intermittently kill each other's connection. fetch-mt5.sh does not take
    # it itself.
    # -w 30: someone is watching a chat. If the terminal is busy that long,
    # saying so beats making them wait.
    # -E 75 gives "could not get the lock" its own exit code, so it is
    # distinguishable from the fetcher failing. Without it the lock case
    # returns 1 with empty output and the model is told "fetch failed" with no
    # detail — which is what happened when a wine process orphaned by an
    # earlier timeout sat holding the lock.
    lock = ["/usr/bin/flock", "-w", "30", "-E", "75", "/tmp/mt5.lock"]

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
    if p1.returncode == 75:
        return {"ok": False, "error": "another MT5 operation holds the lock",
                "detail": "waited 30s for /tmp/mt5.lock",
                "note": "Try again shortly. If it persists, a stale wine "
                        "process may be holding it."}
    if p1.returncode != 0:
        return {"ok": False, "error": "fetch failed", "symbol": symbol,
                "detail": ((p1.stderr or p1.stdout) or "").strip()[-600:]
                          or f"exit {p1.returncode}, no output"}

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
                   "review, so quote absolute P&L from it with that in mind."),
    }


@mcp.tool(
    name="backtests.run",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
@_traced("backtests.run")
def backtests_run(
    strategy: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    params: dict | None = None,
    start: str | None = None,
    end: str | None = None,
    intrabar: str | None = None,
    refresh: bool = True,
    config: str | None = None,
) -> dict:
    """Run a NEW backtest and return its metrics.

    Two ways to call it, and the difference is visible in the result.

    **With `config`** — a name from backtests.list_strategies `configs` — the
    run is exactly what that file specifies: its symbol, timeframe, period and
    execution costs. This is the only way to reach the execution settings;
    starting balance, leverage, spread and commission are not strategy
    parameters and `params` rejects them. Use it whenever someone asks to run
    a stored setup, or to reproduce a run they did themselves.

    **Without `config`** the run uses engine defaults — 10,000 balance, 100:1
    leverage, every bar in the store — and `strategy`, `symbol` and `timeframe`
    are all required. Those defaults are frequently not what the trader has in
    their own config, so a percentage from such a run is not comparable to one
    they produced; the returned `execution` and `period` say what was actually
    used, and reporting a return without them invites exactly that mistake.

    Anything passed explicitly overrides the config, so a config plus
    `params={"take_profit": "mz100"}` is the file with one value changed.

    Report it the same way you report a stored one — same weight, same wording.
    Whether a human has reviewed a run is filing metadata; it is not a caveat
    and does not belong in front of the numbers.

    Two things do belong with the result, because they describe the statistic
    rather than its paperwork:

    - the trade count, next to any rate;
    - `out_of_sample`. False means no `end` held data back, so the result is
      fitted to the whole period it was measured on. Say it once.

    And a parameter the trader chose after seeing earlier results is fitted to
    those results. If they ask you to sweep values until something looks good,
    say what that does to the number rather than just running it.

    Args:
        strategy: from backtests.list_strategies, e.g. "day_open", "sma_cross".
        symbol: e.g. "XAUUSD".
        timeframe: e.g. "M15", "H4".
        params: strategy parameters, e.g. {"stop_pct": 2.0}.
        start: first bar, "YYYY-MM-DD".
        end: last bar. Setting this is what holds later data out of sample.
        intrabar: conservative | optimistic | ohlc.
        refresh: re-download this symbol and timeframe before running, so the
            result covers up to now. On by default, and takes seconds. Pass
            False only to re-run against exactly the data an earlier run used.
    """
    # Before, not only after a "no bars" failure. A store last topped up a week
    # ago silently produces a backtest that stops a week ago, and nothing in
    # the numbers says so — which is the shape of wrong answer this service
    # exists to avoid.
    config_path = None
    if config:
        config_path = _resolve_config(config)
        if config_path is None:
            return {"ok": False, "error": f"no such config {config!r}",
                    "available": [c["config"] for c in _list_configs()],
                    "fix": "call backtests.list_strategies and use a name from `configs`"}
    elif not (strategy and symbol and timeframe):
        return {"ok": False,
                "error": "need either config, or all of strategy, symbol and timeframe"}

    # The refresh has to know what to download, and with a config the answer is
    # in the file rather than the arguments. Explicit arguments still win —
    # they are what the run will use.
    stored = _config_values(config_path) if config_path else {}
    fetch_symbol = symbol or stored.get("symbol")
    fetch_timeframe = timeframe or stored.get("timeframe")
    fetch_start = start or stored.get("start")

    refreshed = None
    if refresh:
        if fetch_symbol and fetch_timeframe:
            refreshed = _refresh_bars(fetch_symbol, fetch_timeframe, fetch_start)
        else:
            refreshed = {"refreshed": False,
                         "why": "could not tell which symbol and timeframe to fetch",
                         "note": ("Bars were NOT refreshed, so this ran on whatever "
                                  "was already stored. Say so when reporting it.")}

    argv = [
        str(_BT_PY), str(_BT_RUNNER),
        "--save", "--runs-dir", "runs-adhoc", "--label", "adhoc",
        "--json", "--quiet",
    ]
    # The config goes on first and every explicit flag folds over it: the
    # runner's own merge only applies flags that were actually given, which is
    # why intrabar is no longer passed unconditionally — doing so silently
    # overrode a config that asked for something else.
    if config_path:
        argv += ["--config", str(config_path)]
    if strategy:
        argv += ["--strategy", strategy]
    if symbol:
        argv += ["--symbol", symbol]
    if timeframe:
        argv += ["--timeframe", timeframe]
    if intrabar:
        argv += ["--intrabar", intrabar]
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
        if refreshed:
            out["data_refresh"] = refreshed
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
                    "config": config, "strategy": strategy, "symbol": symbol,
                    "timeframe": timeframe, "params": params or {},
                    "start": start, "end": end, "intrabar": intrabar,
                },
            }, indent=2) + "\n")
        except OSError:
            pass

    # What was asked for and what ran are not the same thing once a config is
    # involved, and the gap between them is exactly what made a tool run look
    # incomparable to the same config run by hand. Read it back off the saved
    # summary rather than restating the arguments.
    saved = {}
    if run_id:
        try:
            saved = json.loads((runs.ADHOC_DIR / run_id / "summary.json").read_text())
        except (OSError, ValueError):
            pass
    effective_end = end or stored.get("end")

    return {
        "ok": True,
        "validated": False,
        "out_of_sample": bool(effective_end),
        "config": config,
        "execution": saved.get("execution"),
        "period": saved.get("period"),
        "run_id": run_id,
        "pattern_id": runs.pattern_id_for(run_id) if run_id else None,
        **(_report_urls(run_id) if run_id else {}),
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
        "params": params or {},
        "metrics": metrics,
        "data_refresh": refreshed,
        "reporting_note": (
            "Quote the trade count with any rate. "
            + (f"Data after {effective_end} was held out." if effective_end else
               "No end date was set, so the whole tested period is in-sample "
               "— state that once.")
            + " `execution` and `period` are what this run actually used. Give "
              "the balance and the dates with any percentage: the same trades "
              "on a different starting balance produce a different return, and "
              "a default-balance run is not comparable to one from a config. "
              "`validated` is filing metadata: do not lead with it, and do not "
              "call this run exploratory."
        ),
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


# ─────────────────────────── live.* — TEMPORARY ─────────────────────────────
# The spec says the assistant does not execute trades, and §10 rules out trade
# tools. These start and stop a process that trades a strategy by its own
# rules, on the shared test account, on an explicit decision recorded in
# docs/LIVE_TRADING.md, for the POC only. There is still no tool that opens,
# closes or modifies a trade.
#
# Any user may call them while the account is a shared demo. The runner refuses
# a real-money account unless given --allow-real, and nothing here passes it.

_LIVE_ACCOUNT_NOTE = (
    "The live trader trades the shared test account configured in the trading "
    "repo — never the user's own MT5 account. Its balance and positions are not "
    "the user's; mt5.* tools are."
)
_LIVE_START_WAIT = 180          # seconds: terminal login, then the history replay
_live_lock = threading.Lock()   # one start or stop at a time


def _live_config(name: str) -> Path | None:
    """A run config by name, with or without its extension."""
    for candidate in (name, f"{name}.yaml", f"{name}.yml", f"{name}.json"):
        path = _resolve_config(candidate)
        if path is not None:
            return path
    return None


def _live_unknown_config(name: str) -> dict:
    return {"ok": False, "error": f"no run config named {name!r}",
            "configs": [c["config"] for c in _list_configs()]}


def _live_not_installed() -> dict:
    return {"ok": False, "error": "live trading is not installed on this host",
            "detail": "the live-trader unit exists only on the server; nothing was started"}


def _live_summary(path: Path) -> dict:
    return live_sessions.load_summary(live_sessions.config_arg(path),
                                      live_sessions.instance_for(path))


@mcp.tool(name="live.status", annotations=READ_ONLY)
@_traced("live.status")
def live_status(config: str | None = None) -> dict:
    """Whether the live trader is running, and what it holds.

    Call this every time someone asks about the live trader — whether it is
    running, what it is doing, its positions, balance or recent trades — and
    never answer from an earlier call: the runner trades between messages.

    Without `config`, reports every config that has run. Each runner's `text`
    is a finished status message; `condition` is running / starting / stuck /
    restarting / failed / stopped / never started, and `mode` is live, shadow
    or paper. In shadow and paper nothing is sent to the account: positions
    marked `simulated` are the backtest's, not real trades, and must not be
    described as open on the account; likewise `simulated_24h` counts the
    backtest's fills and closes, and only `last_24h` is what traded on the
    account. Pass on `margin_warning` when present.

    This account is the shared test account, not the user's own — say so if
    it could be confused with their account.
    """
    if config:
        path = _live_config(config)
        if path is None:
            return _live_unknown_config(config)
        paths = [path]
    else:
        paths = [p for p in (_live_config(c["config"]) for c in _list_configs()) if p]

    runners = []
    installed = False
    for path in paths:
        instance = live_sessions.instance_for(path)
        unit = live_sessions.unit_state(instance) if instance else None
        installed = installed or unit is not None
        summary = _live_summary(path)
        if config or summary["session"] or live_sessions.unit_active(unit):
            runners.append(summary)
    return {
        "installed": installed,
        "runners": runners,
        "startable_configs": [c["config"] for c in _list_configs()],
        "account_note": _LIVE_ACCOUNT_NOTE,
    }


@mcp.tool(
    name="live.start",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
)
@_traced("live.start")
def live_start(config: str, paper: bool = False) -> dict:
    """Start the live trader on a run config — only when the user asks for it.

    The runner trades the strategy by its own rules on the shared test account
    until stopped, and keeps running across restarts. It starts **live** by
    default; pass `paper=true` only when the user asks for paper or simulated
    trading, and then nothing is ever sent to the account.

    Do not start it on your own initiative, as a suggestion you then act on, or
    to fix something. Do not start it again if it is already running.

    It first replays the config's history, which takes up to a few minutes.
    Report what comes back:
    - `condition: running`, `mode: live` — trading on the account.
    - `mode: shadow` — running, but nothing is sent until the account holds
      what the backtest holds; say that plainly rather than "trading".
    - `ok: false` — it did not start; give `error` / `failure` as they are.
    - `condition: starting` — still replaying; check with live.status later.
    """
    path = _live_config(config)
    if path is None:
        return _live_unknown_config(config)
    instance = live_sessions.instance_for(path)
    if instance is None:
        return {"ok": False, "error": f"{path.name} cannot be run as a unit instance"}

    with _live_lock:
        unit = live_sessions.unit_state(instance)
        if unit is None:
            return _live_not_installed()
        if live_sessions.unit_active(unit):
            return {"ok": False, "error": f"the live trader for {path.name} is already running",
                    "status": _live_summary(path)}

        # Two configs of one strategy and symbol share a magic number, and each
        # would close the other's positions as leftovers.
        values = _config_values(path)
        for other in _list_configs():
            other_path = _live_config(other["config"])
            if other_path is None or other_path == path:
                continue
            other_instance = live_sessions.instance_for(other_path)
            if not other_instance or not live_sessions.unit_active(
                    live_sessions.unit_state(other_instance)):
                continue
            other_state = _live_summary(other_path)
            if values and (other_state["strategy"], other_state["symbol"]) == (
                    values.get("strategy"), values.get("symbol")):
                return {"ok": False,
                        "error": f"{other_path.name} is already trading {values.get('strategy')} "
                                 f"on {values.get('symbol')}; stop it first"}

        requested = datetime.now(timezone.utc).replace(microsecond=0)
        live_sessions.write_env(instance, path, paper)
        try:
            ok, detail = live_sessions.start_unit(instance)
        except live_sessions.UnitUnavailable as exc:
            return {"ok": False, "error": "could not start the unit", "detail": str(exc)}
        if not ok:
            return {"ok": False, "error": "could not start the unit", "detail": detail}

        deadline = time.monotonic() + _LIVE_START_WAIT
        while time.monotonic() < deadline:
            time.sleep(2)
            summary = _live_summary(path)
            started = live_sessions.parse_time(summary["started_at"])
            ours = started is not None and started >= requested
            if ours and summary["condition"] not in ("starting",):
                return {"ok": summary["condition"] in ("running", "stuck"),
                        **({"error": "the live trader did not start"}
                           if summary["condition"] not in ("running", "stuck") else {}),
                        "paper": paper, "status": summary, "account_note": _LIVE_ACCOUNT_NOTE}
            unit = live_sessions.unit_state(instance)
            if not ours and unit is not None and unit["active_state"] == "failed":
                return {"ok": False, "error": "the live trader exited before it could report",
                        "detail": live_sessions.unit_log(instance)}
        return {"ok": True, "paper": paper, "status": _live_summary(path),
                "note": "still connecting or replaying history; check live.status shortly",
                "account_note": _LIVE_ACCOUNT_NOTE}


@mcp.tool(
    name="live.stop",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
)
@_traced("live.stop")
def live_stop(config: str) -> dict:
    """Stop the live trader on a run config — only when the user asks for it.

    Takes up to a minute and a half. Stopping withdraws resting limit orders
    and leaves open positions on the account under their broker-side stops
    and targets: nothing is closed. Say that when positions remain
    (`positions_left_open`).
    """
    path = _live_config(config)
    if path is None:
        return _live_unknown_config(config)
    instance = live_sessions.instance_for(path)
    if instance is None:
        return {"ok": False, "error": f"{path.name} cannot be run as a unit instance"}

    with _live_lock:
        unit = live_sessions.unit_state(instance)
        if unit is None:
            return _live_not_installed()
        if not live_sessions.unit_active(unit):
            if unit.get("enabled"):
                live_sessions.stop_unit(instance)    # a failed instance would come back at boot
            return {"ok": True, "was_running": False, "status": _live_summary(path)}
        try:
            ok, detail = live_sessions.stop_unit(instance)
        except live_sessions.UnitUnavailable as exc:
            return {"ok": False, "error": "could not stop the unit", "detail": str(exc)}
        summary = _live_summary(path)
        if not ok:
            return {"ok": False, "error": "could not stop the unit", "detail": detail,
                    "status": summary}
        left = [] if summary["simulated"] else summary["positions"]
        return {"ok": True, "was_running": True, "positions_left_open": left,
                "status": summary}


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
