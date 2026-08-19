"""Derive POC fixtures from real backtest output in ../trading/runs.

The point of building these rather than hand-writing them is provenance: every
number the assistant can quote traces back to an actual run on this machine. A
fixture that invents "win_rate: 0.57" teaches the agent exactly the habit
SOUL.md forbids, and would make the Phase 4 evals meaningless.

Anything this script derives rather than copies is labelled in the output.

Usage:  .venv/bin/python mcp_server/build_fixtures.py
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO.parent / "trading" / "runs"
OUT = Path(__file__).resolve().parent / "fixtures"

DAY_OPEN = "20260727-130800_day_open_XAUUSD_M15"
ZONES = "20260817-144933_zones_EURUSD_H4_6E_dev2pct"


def _day(iso: str) -> str:
    return iso[:10]


def build_backtests() -> list[dict]:
    """Machine-readable pattern records in the spec §12 shape."""
    patterns = []

    # ---- 1. XAUUSD day-open, a real strategy backtest with trade-level results.
    s = json.loads((RUNS / DAY_OPEN / "summary.json").read_text())
    m, p, ex = s["metrics"], s["params"], s["execution"]

    # expectancy_r: the run reports expectancy in account currency. Expressing it
    # in R needs a risk unit, and the closest defensible one is the average
    # losing trade, since every loss here exits at the same 2% stop. Derived,
    # not measured — flagged as such in `derived` below.
    expectancy_r = round(m["expectancy"] / abs(m["avg_loss"]), 4)

    patterns.append({
        "pattern_id": "XAU_M15_DAY_OPEN",
        "version": 1,
        "instrument": s["symbol"],
        "timeframe": s["timeframe"],
        "tested_from": _day(s["period"]["start"]),
        "tested_to": _day(s["period"]["end"]),
        "sample_size": m["trades"],
        "win_rate": round(m["win_rate_pct"] / 100, 4),
        "expectancy_r": expectancy_r,
        "profit_factor": round(m["profit_factor"], 4),
        "conditions": {
            "entry": "Long at the first bar of the UTC trading day",
            "direction": p["direction"],
            "weekdays": p["weekdays"],
            "one_position_at_a_time": p["one_position"],
        },
        "invalidations": {
            "stop_loss_pct": p["stop_pct"],
            "take_profit_pct": p["take_pct"],
        },
        "execution_assumptions": {
            "initial_balance": ex["initial_balance"],
            "slippage_points": ex["slippage_points"],
            "commission_per_lot": ex["commission_per_lot"],
            "swap_applied": ex["apply_swap"],
            "intrabar": ex["intrabar"],
        },
        "limitations": [
            "Single instrument, single direction — long only.",
            f"Max drawdown {m['max_drawdown_pct']:.1f}% over "
            f"{m['max_drawdown_days']:.0f} days.",
            f"Longest losing streak: {m['max_consecutive_losses']} trades.",
            "No out-of-sample split; the whole period is in-sample.",
            "Payoff ratio below 1 — profitability depends on the win rate holding.",
        ],
        "detector_version": None,   # no live detector yet — spec §14
        "backtest_run_id": DAY_OPEN,
        "derived": {
            "expectancy_r": "expectancy / |avg_loss|; the run reports currency, not R",
        },
        "report_available": True,
    })

    # ---- 2. EURUSD margin zones. Structural analysis, NOT a traded strategy:
    # it has no trade list, so it must not carry a win rate or expectancy. Left
    # absent rather than zero — the agent needs to see a record that genuinely
    # cannot answer "what is the edge".
    z = json.loads((RUNS / ZONES / "summary.json").read_text())
    patterns.append({
        "pattern_id": "EUR_H4_MARGIN_ZONES",
        "version": 1,
        "instrument": z["symbol"],
        "timeframe": z["timeframe"],
        "tested_from": _day(z["period"]["start"]),
        "tested_to": _day(z["period"]["end"]),
        "sample_size": z["zones"]["count"],
        "win_rate": None,
        "expectancy_r": None,
        "profit_factor": None,
        "conditions": {
            "zigzag_deviation": z["zigzag"]["deviation"],
            "initial_margin_ratio": z["zones"]["initial_ratio"],
            "contract": z["contract"]["code"],
        },
        "observations": {
            "zones_identified": z["zones"]["count"],
            "reached_first_margin_zone_pct": z["zones"]["reached_fmz_pct"],
            "reached_intermediate_zone_pct": z["zones"]["reached_imz_pct"],
            "avg_first_zone_distance_pips": z["zones"]["avg_fmz_pips"],
            "rollover_crossings_true": z["crossings"]["true"],
            "rollover_crossings_false": z["crossings"]["false"],
        },
        "limitations": [
            "Structural study, not a traded strategy: no entries, exits or P&L.",
            "Reach rates are not win rates. A zone being reached says nothing "
            "about whether trading toward it was profitable.",
            "No execution assumptions modelled — no spread, slippage or costs.",
        ],
        "detector_version": None,
        "backtest_run_id": ZONES,
        "report_available": True,
    })

    return patterns


def build_trade_history(limit: int = 40) -> list[dict]:
    """Recent closed trades, taken verbatim from the real backtest trade log."""
    rows = list(csv.DictReader((RUNS / DAY_OPEN / "trades.csv").open()))[-limit:]
    out = []
    for r in rows:
        out.append({
            "ticket": int(r["id"]),
            "symbol": "XAUUSD",
            "direction": r["side"],
            "volume": float(r["volume"]),
            "open_time": r["entry_time"],
            "open_price": float(r["entry_price"]),
            "close_time": r["exit_time"],
            "close_price": float(r["exit_price"]),
            "close_reason": r["reason"],
            "profit": round(float(r["net_pnl"]), 2),
            "swap": float(r["swap"]),
            "commission": float(r["commission"]),
            "balance_after": round(float(r["balance_after"]), 2),
        })
    return out


# Series that back the charts. Copied into fixtures so the MCP server stays
# self-contained — a real deployment will not have ../trading/runs mounted.
MAX_SERIES_ROWS = 500

SERIES = {
    ZONES: {
        "pivots": "Swing highs and lows identified by the zigzag.",
        "envelopes": "Margin-zone envelopes per pivot, with reach flags.",
        "crossings": "Rollover crossings of the 50% level, true vs false.",
        "rollover": "Daily rollover reference prices.",
    },
    DAY_OPEN: {
        "trades": "Closed trades with entry, exit, reason and P&L.",
        "equity": "Equity curve.",
    },
}


def _read_csv(path: Path) -> list[dict]:
    """CSV rows with numeric-looking fields coerced, so the agent gets numbers
    rather than strings it might quote verbatim."""
    out = []
    for row in csv.DictReader(path.open()):
        rec = {}
        for k, v in row.items():
            if v in ("True", "False"):
                rec[k] = v == "True"
            else:
                try:
                    rec[k] = int(v) if v.lstrip("-").isdigit() else float(v)
                except (ValueError, AttributeError):
                    rec[k] = v
        out.append(rec)
    return out


def build_series() -> dict:
    out = {}
    for run_id, names in SERIES.items():
        for name, description in names.items():
            f = RUNS / run_id / f"{name}.csv"
            if not f.exists():
                continue
            rows = _read_csv(f)
            total = len(rows)

            # An equity curve is one row per bar — 100k of them. Nobody, model
            # or human, benefits from the full series, and it would dominate
            # the fixture file. Downsample evenly and say so, rather than
            # silently truncating the tail and misrepresenting the shape.
            note = None
            if total > MAX_SERIES_ROWS:
                step = total // MAX_SERIES_ROWS + 1
                rows = rows[::step] + rows[-1:]
                note = (
                    f"Downsampled from {total} rows to {len(rows)} "
                    f"(every {step}th, plus the final row)."
                )

            out[f"{run_id}::{name}"] = {
                "run_id": run_id,
                "series": name,
                "description": description,
                "columns": list(rows[0].keys()) if rows else [],
                "row_count": len(rows),
                "source_row_count": total,
                "downsampled": note,
                "rows": rows,
            }
    return out


def build_account(history: list[dict]) -> dict:
    last = history[-1]
    return {
        "account_id": "acct_poc_001",
        "nickname": "FTMO 100K Challenge",
        "broker": "FTMO",
        "server": "FTMO-Demo2",
        "login": 10534821,
        "currency": "USD",
        "leverage": 100,
        "access": "investor_read_only",
        "balance": last["balance_after"],
        "equity": last["balance_after"],
        "margin": 0.0,
        "free_margin": last["balance_after"],
        "margin_level": None,
        "open_positions": 0,
        "as_of": last["close_time"],
        "connection_state": "CONNECTED",
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    history = build_trade_history()
    payloads = {
        "backtests.json": build_backtests(),
        "trade_history.json": history,
        "account.json": build_account(history),
        # No open positions: the empty case has to be representable too.
        "positions.json": [],
        "series.json": build_series(),
    }
    for name, data in payloads.items():
        (OUT / name).write_text(json.dumps(data, indent=2) + "\n")
        n = len(data) if isinstance(data, list) else 1
        print(f"wrote {name} ({n} record{'s' if n != 1 else ''})")


if __name__ == "__main__":
    main()
