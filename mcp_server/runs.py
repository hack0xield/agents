"""Read backtest results straight from the backtester's run directory.

Replaces a generated fixture snapshot. A snapshot goes stale the moment a new
backtest is run, and an assistant whose whole claim is "I know what we have
tested" must not be answering from a copy someone forgot to regenerate.

Two run shapes exist and they are not interchangeable:

  strategy  — has metrics.trades: a traded backtest with entries, exits and P&L.
  study     — has zones/crossings: structural analysis with no trade list. It
              cannot answer "what is the edge", and must never be given a
              win rate.

Re-running the same configuration produces a new run directory rather than
overwriting the old one, which is exactly the immutability spec §13 asks for.
Those become versions of one pattern, ordered by run timestamp.
"""

from __future__ import annotations

import csv
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_TRADING = Path(__file__).resolve().parent.parent.parent / "trading"

# Reviewed, published backtests. These are the proprietary asset (spec §12).
RUNS_DIR = Path(os.environ.get("TRADING_RUNS_DIR", _TRADING / "runs"))

# Ad-hoc runs the assistant executed on request. Kept in a separate directory
# so a backtest produced 4 seconds ago to answer a question is never confused
# with one that was designed, reviewed and published. Every record carries
# `validated`, and the agent is required to say which it is looking at.
ADHOC_DIR = Path(os.environ.get("TRADING_ADHOC_RUNS_DIR", _TRADING / "runs-adhoc"))

MAX_SERIES_ROWS = 500

# 20260817-144933_zones_EURUSD_H4_6E_dev2pct -> ZONES_EURUSD_H4_6E_DEV2PCT
_RUN_NAME = re.compile(r"^(\d{8}-\d{6})_(.+)$")


def pattern_id_for(run_name: str) -> str | None:
    m = _RUN_NAME.match(run_name)
    if not m:
        return None
    return re.sub(r"[^A-Z0-9]+", "_", m.group(2).upper()).strip("_")


def run_timestamp(run_name: str) -> str:
    m = _RUN_NAME.match(run_name)
    return m.group(1) if m else ""


def _day(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def _dir_signature() -> tuple:
    """Cheap fingerprint of both run directories, so the index rebuilds when a
    new backtest lands but not on every single tool call."""
    out = []
    for validated, root in ((True, RUNS_DIR), (False, ADHOC_DIR)):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            s = d / "summary.json"
            if s.is_file():
                out.append((d.name, s.stat().st_mtime_ns, validated))
    return tuple(out)


def _provenance(run_dir: Path) -> dict | None:
    """Who initiated this run and whether anyone reviewed it.

    Absent on runs made before this was recorded, and on anything produced
    directly by the backtester CLI — those are treated as belonging to whoever
    ran them, which is the directory's job to say.
    """
    f = run_dir / "provenance.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def _normalize(run_name: str, summary: dict, version: int) -> dict:
    """One run -> one pattern version, in the spec §12 machine-readable shape."""
    period = summary.get("period", {})
    base = {
        "pattern_id": pattern_id_for(run_name),
        "version": version,
        "instrument": summary.get("symbol"),
        "timeframe": summary.get("timeframe"),
        "tested_from": _day(period.get("start")),
        "tested_to": _day(period.get("end")),
        "backtest_run_id": run_name,
        "detector_version": None,   # no live detector yet — spec §14
        "report_available": True,
    }

    m = summary.get("metrics")
    if m and m.get("trades"):
        # Traded strategy. expectancy_r is derived: the run reports expectancy
        # in account currency, and the closest defensible risk unit is the
        # average losing trade. Flagged under `derived` so it is never mistaken
        # for something the backtester measured.
        avg_loss = abs(m.get("avg_loss") or 0) or None
        p = summary.get("params", {})
        ex = summary.get("execution", {})
        base.update({
            "kind": "strategy",
            "sample_size": m["trades"],
            "win_rate": round(m["win_rate_pct"] / 100, 4) if m.get("win_rate_pct") is not None else None,
            "expectancy_r": round(m["expectancy"] / avg_loss, 4) if avg_loss and m.get("expectancy") is not None else None,
            "profit_factor": round(m["profit_factor"], 4) if m.get("profit_factor") is not None else None,
            "conditions": {k: v for k, v in p.items() if v is not None},
            "invalidations": {
                "stop_loss_pct": p.get("stop_pct"),
                "take_profit_pct": p.get("take_pct"),
            },
            "execution_assumptions": {
                "initial_balance": ex.get("initial_balance"),
                "slippage_points": ex.get("slippage_points"),
                "commission_per_lot": ex.get("commission_per_lot"),
                "swap_applied": ex.get("apply_swap"),
                "intrabar": ex.get("intrabar"),
            },
            "limitations": _strategy_limitations(m, ex),
            "derived": {
                "expectancy_r": "expectancy / |avg_loss|; the run reports currency, not R",
            },
        })
        return base

    z = summary.get("zones")
    if z:
        base.update({
            "kind": "study",
            "sample_size": z.get("count"),
            # A structural study has no trades. These stay null rather than
            # zero: null says "cannot be computed", zero says "we measured, and
            # it was nothing".
            "win_rate": None,
            "expectancy_r": None,
            "profit_factor": None,
            "conditions": {
                "zigzag_deviation": summary.get("zigzag", {}).get("deviation"),
                "initial_margin_ratio": z.get("initial_ratio"),
                "contract": summary.get("contract", {}).get("code"),
            },
            "observations": {
                "zones_identified": z.get("count"),
                "reached_first_margin_zone_pct": z.get("reached_fmz_pct"),
                "reached_intermediate_zone_pct": z.get("reached_imz_pct"),
                "avg_first_zone_distance_pips": z.get("avg_fmz_pips"),
                "rollover_crossings_true": summary.get("crossings", {}).get("true"),
                "rollover_crossings_false": summary.get("crossings", {}).get("false"),
            },
            "limitations": [
                "Structural study, not a traded strategy: no entries, exits or P&L.",
                "Reach rates are not win rates. A zone being reached says nothing "
                "about whether trading toward it was profitable.",
                "No execution assumptions modelled — no spread, slippage or costs.",
            ],
        })
        return base

    base.update({"kind": "unknown", "sample_size": None, "win_rate": None,
                 "expectancy_r": None, "profit_factor": None,
                 "limitations": ["Unrecognised run shape; metrics not interpreted."]})
    return base


def _strategy_limitations(m: dict, ex: dict) -> list[str]:
    out = []
    if m.get("max_drawdown_pct") is not None:
        out.append(
            f"Max drawdown {m['max_drawdown_pct']:.1f}% over "
            f"{m.get('max_drawdown_days', 0):.0f} days."
        )
    if m.get("max_consecutive_losses"):
        out.append(f"Longest losing streak: {m['max_consecutive_losses']} trades.")
    if not ex.get("slippage_points") and not ex.get("commission_per_lot"):
        out.append("Zero slippage and zero commission assumed; real fills will be worse.")
    if m.get("payoff_ratio") is not None and m["payoff_ratio"] < 1:
        out.append(
            f"Payoff ratio {m['payoff_ratio']:.2f} (<1) — profitability depends on "
            "the win rate holding."
        )
    out.append("No out-of-sample split; the whole period is in-sample.")
    return out


@lru_cache(maxsize=1)
def _index(_signature: tuple) -> list[dict]:
    """All runs, normalized, with versions assigned per pattern by run time.

    Validated and ad-hoc runs are versioned independently: an exploratory run
    must never become "v9" of a reviewed pattern and inherit its standing.
    """
    groups: dict[tuple[str, bool], list[str]] = {}
    for name, _, validated in _signature:
        pid = pattern_id_for(name)
        if pid:
            groups.setdefault((pid, validated), []).append(name)

    records = []
    for (pid, validated), names in groups.items():
        root = RUNS_DIR if validated else ADHOC_DIR
        for version, name in enumerate(sorted(names, key=run_timestamp), start=1):
            try:
                summary = json.loads((root / name / "summary.json").read_text())
            except (OSError, ValueError):
                continue
            rec = _normalize(name, summary, version)
            # Provenance in the run wins over the directory it sits in. The
            # directory is organisation; provenance is fact. A run the
            # assistant produced and a human later reviewed can be marked so
            # without moving files around.
            prov = _provenance(root / name)
            rec["validated"] = prov["reviewed"] if prov else validated
            rec["provenance"] = prov
            rec["runs_root"] = str(root)
            records.append(rec)
    return records


def all_patterns() -> list[dict]:
    return _index(_dir_signature())


def latest_versions() -> list[dict]:
    """Newest version of each pattern — what a search should surface by default,
    rather than eight near-identical reruns of the same study.

    Validated and ad-hoc are separate entries, never merged.
    """
    best: dict[tuple[str, bool], dict] = {}
    for r in all_patterns():
        k = (r["pattern_id"], r["validated"])
        if k not in best or r["version"] > best[k]["version"]:
            best[k] = r
    return list(best.values())


def find(pattern_id: str, version: int | None = None,
         validated: bool | None = None) -> dict | None:
    matches = [r for r in all_patterns() if r["pattern_id"].upper() == pattern_id.upper()]
    if validated is not None:
        matches = [r for r in matches if r["validated"] is validated]
    if not matches:
        return None
    if version is None:
        return max(matches, key=lambda r: r["version"])
    for r in matches:
        if r["version"] == version:
            return r
    return None


def version_count(pattern_id: str, validated: bool | None = None) -> int:
    rows = [r for r in all_patterns() if r["pattern_id"].upper() == pattern_id.upper()]
    if validated is not None:
        rows = [r for r in rows if r["validated"] is validated]
    return len(rows)


def run_root_for(run_id: str) -> Path | None:
    for root in (RUNS_DIR, ADHOC_DIR):
        if (root / run_id / "summary.json").is_file():
            return root
    return None


def series_names(run_id: str) -> list[str]:
    root = run_root_for(run_id) or RUNS_DIR
    d = root / run_id
    if not d.is_dir():
        return []
    return sorted(f.stem for f in d.glob("*.csv"))


def _coerce(v: str) -> Any:
    if v in ("True", "False"):
        return v == "True"
    try:
        return int(v) if v.lstrip("-").isdigit() else float(v)
    except (ValueError, AttributeError):
        return v


def read_series(run_id: str, name: str) -> dict | None:
    """Rows of one CSV, downsampled if very long.

    Downsampling is even rather than a head/tail cut: truncating an equity curve
    would silently misrepresent its shape, which is worse than saying it is
    abridged.
    """
    root = run_root_for(run_id) or RUNS_DIR
    f = root / run_id / f"{name}.csv"
    if not f.is_file():
        return None
    rows = [{k: _coerce(v) for k, v in r.items()} for r in csv.DictReader(f.open())]
    total = len(rows)
    note = None
    if total > MAX_SERIES_ROWS:
        step = total // MAX_SERIES_ROWS + 1
        rows = rows[::step] + rows[-1:]
        note = f"Downsampled from {total} rows to {len(rows)} (every {step}th, plus the final row)."
    return {
        "series": name,
        "columns": list(rows[0].keys()) if rows else [],
        "row_count": len(rows),
        "source_row_count": total,
        "downsampled": note,
        "rows": rows,
    }
