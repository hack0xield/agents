"""Live traders: the systemd unit that runs one, the files it writes, and the
messages that report on it.

A live trader is `trading/scripts/run-live.sh` trading one run config on the
shared test account, as the user unit `live-trader@<config stem>`. It keeps
`runs-live/<strategy>_<symbol>_<magic>/state.json` and `events.jsonl`, whose
fields are specified by the trading README, "The session, as reporting reads
it". The live.* MCP tools and apps/live_notify both read them through here.

Standard library only: apps/live_notify imports this from another venv path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRADING_ROOT = Path(__file__).resolve().parent.parent.parent / "trading"
LIVE_DIR = TRADING_ROOT / "runs-live"
CONFIG_DIR = TRADING_ROOT / "configs" / "strategies"
ENV_DIR = Path.home() / ".config" / "trading-assistant" / "live"

#: A running trader rewrites state.json every poll (2 s). Longer than this
#: without a write, it is stuck.
HEARTBEAT_STALE_SECONDS = 120

#: Event kinds a person is told about as they happen. The rest are either
#: followed within a second by their answer (the intents) or feed the daily
#: status (bar_closed).
NOTIFY_KINDS = {
    "order_filled", "order_placed", "order_cancelled", "order_rejected",
    "position_modified", "position_closed", "started", "mode", "stopped", "error",
}

_INSTANCE = re.compile(r"^[A-Za-z0-9_-]+$")


# ─────────────────────────────── configs and units ──────────────────────────

def instance_for(config: Path) -> str | None:
    """The unit instance name for a config file, or None if its name cannot be one."""
    return config.stem if _INSTANCE.match(config.stem) else None


def config_arg(config: Path) -> str:
    """The config as run-live.sh is given it, and as state.json records it."""
    return f"configs/strategies/{config.name}"


def unit_name(instance: str) -> str:
    return f"live-trader@{instance}.service"


class UnitUnavailable(RuntimeError):
    """systemctl could not be run or did not answer."""


def _systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnitUnavailable(f"systemctl {' '.join(args)}: {exc}") from exc


def unit_state(instance: str) -> dict | None:
    """The unit's systemd state, or None where the unit is not installed."""
    try:
        proc = _systemctl("show", unit_name(instance), "-p",
                          "LoadState,ActiveState,SubState,Result,NRestarts,"
                          "ActiveEnterTimestamp,UnitFileState")
    except UnitUnavailable:
        return None
    props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if proc.returncode != 0 or props.get("LoadState") != "loaded":
        return None
    return {
        "active_state": props.get("ActiveState"),
        "sub_state": props.get("SubState"),
        "result": props.get("Result"),
        "restarts": int(props.get("NRestarts") or 0),
        "since": props.get("ActiveEnterTimestamp") or None,
        "enabled": props.get("UnitFileState") == "enabled",
    }


def unit_active(unit: dict | None) -> bool:
    return bool(unit) and unit["active_state"] in ("active", "activating", "reloading")


def write_env(instance: str, config: Path, paper: bool, env_dir: Path | None = None) -> Path:
    """What the instance runs. `--allow-real` is never among its arguments."""
    env_dir = env_dir or ENV_DIR
    env_dir.mkdir(parents=True, exist_ok=True)
    path = env_dir / f"{instance}.env"
    path.write_text(
        "# Written by the live.start tool. The unit reads it on every start.\n"
        f"LIVE_CONFIG={config_arg(config)}\n"
        f"LIVE_ARGS={'--paper' if paper else ''}\n",
        encoding="utf-8",
    )
    return path


def start_unit(instance: str) -> tuple[bool, str]:
    """Start the instance and enable it, so it comes back after a reboot."""
    _systemctl("reset-failed", unit_name(instance))
    proc = _systemctl("enable", "--now", unit_name(instance), timeout=60)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def stop_unit(instance: str) -> tuple[bool, str]:
    """Stop the instance and disable it. Blocks through the runner's clean stop."""
    proc = _systemctl("disable", "--now", unit_name(instance), timeout=150)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def unit_log(instance: str, lines: int = 15) -> str:
    try:
        proc = subprocess.run(["journalctl", "--user", "-u", unit_name(instance), "-n",
                               str(lines), "--no-pager", "-o", "cat"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()[-1500:]


# ─────────────────────────────── session files ──────────────────────────────

def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sessions(live_dir: Path | None = None) -> list[Path]:
    """Every session directory holding a state.json or an events.jsonl."""
    live_dir = live_dir or LIVE_DIR
    if not live_dir.is_dir():
        return []
    return sorted(d for d in live_dir.iterdir()
                  if d.is_dir() and ((d / "state.json").exists() or (d / "events.jsonl").exists()))


def session_for(config: str, live_dir: Path | None = None) -> Path | None:
    """The session a config last ran as: the newest state.json naming it."""
    best, best_time = None, None
    for directory in sessions(live_dir):
        state = read_json(directory / "state.json")
        if not state or state.get("config") != config:
            continue
        updated = parse_time(state.get("updated_at"))
        if best is None or (updated and (best_time is None or updated > best_time)):
            best, best_time = directory, updated
    return best


def entries(path: Path, offset: int) -> list[tuple[dict | None, int]]:
    """Each complete line after `offset`, parsed, with the offset just past it.

    An unreadable line comes back as None, so a reader can step over it. A line
    still being written is left for the next call. A file shorter than the
    offset was replaced, and is read from the start.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size < offset:
        offset = 0
    if size == offset:
        return []
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    out = []
    position = 0
    while True:
        newline = chunk.find(b"\n", position)
        if newline < 0:
            return out
        line = chunk[position:newline]
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        out.append((event if isinstance(event, dict) else None, offset + newline + 1))
        position = newline + 1


def tail(path: Path, offset: int) -> tuple[list[dict], int]:
    """Complete events after `offset`, and the offset past them."""
    found = entries(path, offset)
    return [e for e, _ in found if e is not None], (found[-1][1] if found else offset)


def events_since(path: Path, since: datetime) -> list[dict]:
    events, _ = tail(path, 0)
    return [e for e in events if (parse_time(e.get("time")) or since) >= since]


# ─────────────────────────────── the summary ────────────────────────────────

def summarize(state: dict | None, events: list[dict], unit: dict | None,
              now: datetime | None = None, config: str | None = None) -> dict:
    """Everything a status report says, from a session's files and its unit.

    `events` are the last 24 hours'. `unit` is None where systemd cannot be
    asked, and then only the files decide.
    """
    now = now or datetime.now(timezone.utc)
    state = state or {}
    updated = parse_time(state.get("updated_at"))
    heartbeat_age = int((now - updated).total_seconds()) if updated else None
    running = bool(state.get("running"))

    if not state:
        condition = "starting" if unit_active(unit) else "never started"
    elif running and unit is not None and not unit_active(unit):
        condition = "ended without a final report"
    elif running and state.get("mode") is None:
        condition = "starting"
    elif running and heartbeat_age is not None and heartbeat_age > HEARTBEAT_STALE_SECONDS:
        condition = "stuck"
    elif running:
        condition = "running"
    elif unit is not None and unit["active_state"] == "activating":
        condition = "restarting"
    elif state.get("failure"):
        condition = "failed"
    else:
        condition = "stopped"

    if state.get("paper"):
        mode = "paper"
    else:
        mode = state.get("mode")

    # Only what happened on the account counts as trading. In shadow the fills
    # and closes are the backtest's, and are kept apart so they cannot read as
    # real trades; errors are the runner's own, whatever its mode.
    live = [e for e in events if e.get("mode") == "live" or e.get("kind") == "error"]
    simulated = [e for e in events if e.get("mode") == "shadow" and e.get("kind") != "error"]

    margin = state.get("margin")
    margin_warning = None
    if margin and margin.get("stale"):
        if margin.get("as_of"):
            margin_warning = (f"Margin data is {margin.get('age_days')} days old "
                              f"({margin.get('contract')}, {margin.get('as_of')}) — zones may "
                              f"use an outdated margin.")
        else:
            margin_warning = (f"No margin reading for {margin.get('contract')} — zones "
                              f"cannot be trusted.")

    summary = {
        "config": config or state.get("config"),
        "condition": condition,
        "mode": mode,
        "simulated": state.get("source") == "replay" or bool(state.get("paper")),
        "strategy": state.get("strategy"),
        "symbol": state.get("symbol"),
        "timeframe": state.get("timeframe"),
        "account": state.get("account"),
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "heartbeat_age_seconds": heartbeat_age,
        "last_closed_bar": state.get("last_closed_bar"),
        "balance": state.get("balance"),
        "equity": state.get("equity"),
        "positions": state.get("positions") or [],
        "resting_orders": state.get("resting_orders") or [],
        "queued_orders": state.get("queued_orders") or [],
        "last_24h": _count(live),
        "simulated_24h": _count(simulated),
        "last_error": state.get("last_error"),
        "failure": state.get("failure"),
        "margin": margin,
        "margin_warning": margin_warning,
        "unit": unit,
    }
    summary["text"] = status_text(summary, now)
    return summary


def _count(events: list[dict]) -> dict:
    counts = {"filled": 0, "closed": 0, "net_pnl": 0.0, "rejected": 0, "cancelled": 0,
              "errors": 0}
    for event in events:
        kind = event.get("kind")
        if kind == "order_filled":
            counts["filled"] += 1
        elif kind == "position_closed" and not event.get("leftover"):
            counts["closed"] += 1
            counts["net_pnl"] += float(event.get("net_pnl") or 0.0)
        elif kind == "order_rejected":
            counts["rejected"] += 1
        elif kind == "order_cancelled":
            counts["cancelled"] += 1
        elif kind == "error":
            counts["errors"] += 1
    counts["net_pnl"] = round(counts["net_pnl"], 2)
    return counts


def last_event_time(path: Path) -> datetime | None:
    """When the newest complete event in an events.jsonl was written."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            chunk = fh.read()
    except OSError:
        return None
    for line in reversed(chunk.split(b"\n")):
        try:
            return parse_time(json.loads(line).get("time"))
        except (ValueError, AttributeError):
            continue
    return None


def snapshot_behind(state: dict | None, events_path: Path) -> bool:
    """True while a runner has written events its state.json does not show yet.

    A runner writes state.json after each step, and a step can take a while —
    a send the broker is slow to answer holds it — so for a moment the events
    say more than the snapshot does.
    """
    updated = parse_time((state or {}).get("updated_at"))
    last = last_event_time(events_path)
    return bool(updated and last and last > updated)


def load_summary(config: str, instance: str | None, live_dir: Path | None = None,
                 now: datetime | None = None, ask_systemd: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    directory = session_for(config, live_dir)
    state = read_json(directory / "state.json") if directory else None
    events = events_since(directory / "events.jsonl", now - timedelta(hours=24)) if directory else []
    unit = unit_state(instance) if (instance and ask_systemd) else None
    summary = summarize(state, events, unit, now, config)
    summary["session"] = directory.name if directory else None
    return summary


# ─────────────────────────────── the messages ───────────────────────────────

def _px(value) -> str:
    return "—" if value is None else f"{float(value):.6g}"


def _money(value) -> str:
    return "—" if value is None else f"{float(value):,.2f}"


def _signed(value) -> str:
    return "—" if value is None else f"{float(value):+,.2f}"


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _stamp(value) -> str:
    moment = parse_time(value)
    return moment.strftime("%d %b %H:%M") if moment else "—"


def _levels(sl, tp) -> str:
    return f"SL {_px(sl)} · TP {_px(tp)}"


def _order(d: dict) -> str:
    """An entry as the chat reads it: side, volume, and a limit or at market."""
    where = f"limit {_px(d.get('limit'))}" if d.get("limit") is not None else "at market"
    return f"{d.get('side')} {d.get('volume')} {where}"


def _title(summary_or_event: dict) -> str:
    parts = [p for p in (summary_or_event.get("symbol"), summary_or_event.get("timeframe")) if p]
    return f"{summary_or_event.get('strategy') or 'live trader'} · {' '.join(parts)}".strip(" ·")


def status_text(s: dict, now: datetime | None = None, label: str = "status") -> str:
    now = now or datetime.now(timezone.utc)
    lines = [f"{_title(s)} — {label}, {now:%d %b %H:%M} UTC"]
    condition = s["condition"]
    mode = (s["mode"] or "").upper()

    if condition == "never started":
        lines.append("Runner   never started")
        return "\n".join(lines)

    if condition in ("running", "stuck"):
        started = parse_time(s["started_at"])
        uptime = f"running {_span((now - started).total_seconds())}" if started else "running"
        beat = s["heartbeat_age_seconds"]
        lines.append(f"Runner   {uptime} · {mode} · heartbeat {_span(beat)} ago · "
                     f"last bar {_stamp(s['last_closed_bar'])} broker time")
        if condition == "stuck":
            lines.insert(1, f"⚠ No heartbeat for {_span(beat)} — the runner may be stuck.")
    elif condition == "starting":
        lines.append("Runner   starting — connecting to the terminal and replaying history")
    elif condition == "restarting":
        lines.append(f"Runner   restarting after a failure: {s['failure'] or 'see the log'}")
    elif condition == "ended without a final report":
        lines.append("⚠ Runner   not running, and it ended without writing a final report")
    elif condition == "failed":
        lines.append(f"⚠ Runner   stopped {_stamp(s['stopped_at'])} UTC after a failure: "
                     f"{s['failure']}")
    else:
        lines.append(f"Runner   stopped {_stamp(s['stopped_at'])} UTC on request")

    account = s.get("account") or {}
    where = f" ({account.get('trade_mode')}, {account.get('server')})" if account else ""
    lines.append(f"Account  balance {_money(s['balance'])} · equity {_money(s['equity'])}"
                 f"{where}")

    if s["simulated"]:
        lines.append("Simulated — the positions and orders below are the backtest's, "
                     "not the account's")
    if s["positions"]:
        for p in s["positions"]:
            profit = f" · {_signed(p.get('profit'))}" if p.get("profit") is not None else ""
            lines.append(f"Open     {p.get('side')} {p.get('volume')} @ {_px(p.get('price'))} · "
                         f"{_levels(p.get('sl'), p.get('tp'))}{profit}")
    else:
        lines.append("Open     none")
    if s["resting_orders"]:
        for o in s["resting_orders"]:
            lines.append(f"Resting  {o.get('side')} {o.get('volume')} limit "
                         f"{_px(o.get('limit'))} · {_levels(o.get('sl'), o.get('tp'))}")
    else:
        lines.append("Resting  none")

    c = s["last_24h"]
    closed = f"{c['closed']} closed ({_signed(c['net_pnl'])})" if c["closed"] else "0 closed"
    lines.append(f"Last 24h {c['filled']} filled · {closed} · {c['rejected']} rejected · "
                 f"{c['errors']} errors")
    b = s["simulated_24h"]
    if b["filled"] or b["closed"]:
        closed = f"{b['closed']} closed ({_signed(b['net_pnl'])})" if b["closed"] else "0 closed"
        lines.append(f"Simulated 24h {b['filled']} filled · {closed} — the backtest's, "
                     f"not on the account")
    if s.get("last_error") and c["errors"]:
        lines.append(f"Last error  {s['last_error'].get('error')}")
    if s["margin_warning"]:
        lines.append(f"⚠ {s['margin_warning']}")
    return "\n".join(lines)


#: The runner's own words for why an order ended, in the trader's.
_WHY = {
    "void level reached": "the price reached the level that voids it",
    "cancelled by strategy": "the strategy withdrew it",
    "runner stopped": "the runner stopped, and a resting order needs it running",
}


def _why(reason) -> str:
    return _WHY.get(str(reason), str(reason))


_REASONS = {"STOP_LOSS": "stop loss", "TAKE_PROFIT": "take profit", "STRATEGY": "strategy exit",
            "SESSION_END": "session end", "END_OF_DATA": "end of data",
            "MARGIN_CALL": "margin call"}


def event_text(event: dict) -> str | None:
    """How an event reads in a chat, or None for a kind nobody is told about."""
    kind = event.get("kind")
    if kind not in NOTIFY_KINDS:
        return None
    d = event
    simulated = d.get("mode") == "shadow" and kind not in ("started", "mode", "stopped", "error")
    head = f"{_title(d)} · {'simulated' if simulated else 'live'}"

    if kind == "order_filled":
        body = ["order filled",
                f"{d.get('side')} {d.get('volume')} @ {_px(d.get('price'))} · "
                f"{_levels(d.get('sl'), d.get('tp'))}"]
    elif kind == "order_placed":
        body = ["limit order placed",
                f"{d.get('side')} {d.get('volume')} limit {_px(d.get('limit'))} · "
                f"{_levels(d.get('sl'), d.get('tp'))}"]
    elif kind == "order_cancelled":
        body = ["order cancelled", f"{_order(d)} — {_why(d.get('reason'))}"]
    elif kind == "order_rejected":
        if d.get("action"):
            body = [f"{d.get('action')} rejected", f"ticket {d.get('ticket')} — {d.get('reason')}"]
        else:
            body = ["order rejected", f"{_order(d)} — {d.get('reason')}"]
    elif kind == "position_modified":
        moves = [f"{name} {_px(d.get(f'{key}_from'))} → {_px(d.get(key))}"
                 for name, key in (("SL", "sl"), ("TP", "tp")) if d.get(f"{key}_from") != d.get(key)]
        body = ["stop or target moved", " · ".join([f"ticket {d.get('ticket')}", *moves])]
    elif kind == "position_closed":
        if d.get("leftover"):
            body = ["closed a position that was not the strategy's",
                    f"{d.get('side')} {d.get('volume')} · ticket {d.get('ticket')}"]
        else:
            reason = _REASONS.get(str(d.get("reason")), str(d.get("reason")).lower())
            body = ["position closed",
                    f"{d.get('side')} {d.get('volume')} · {_px(d.get('entry_price'))} → "
                    f"{_px(d.get('exit_price'))} · {reason} · net {_signed(d.get('net_pnl'))}"]
    elif kind == "started":
        head = _title(d)
        account = d.get("account") or {}
        if d.get("paper"):
            how = "paper — nothing will be sent to the account"
        elif account:
            how = f"on the shared {account.get('trade_mode')} account ({account.get('server')})"
        else:
            how = "on the shared account"
        body = ["runner started", how,
                f"replayed {d.get('replay_trades')} trades since {str(d.get('replay_from'))[:10]}"]
        margin = d.get("margin") or {}
        if margin.get("stale"):
            body.append(f"⚠ margin data {margin.get('age_days')} days old "
                        f"({margin.get('contract')}, {margin.get('as_of')})")
    elif kind == "mode":
        head = _title(d)
        body = ["now trading live"]
        adopted = len(d.get("adopted_positions") or []) + len(d.get("adopted_orders") or [])
        cleared = len(d.get("closed_leftovers") or []) + len(d.get("removed_leftovers") or [])
        if adopted:
            body.append(f"took over {adopted} position(s) or order(s) already on the account")
        if cleared:
            body.append(f"closed or removed {cleared} that were not the strategy's")
        for order in d.get("queued") or []:
            body.append(f"sending {_order(order)}")
    elif kind == "stopped":
        head = _title(d)
        body = [f"runner stopped after a failure: {d['failure']}" if d.get("failure")
                else "runner stopped",
                "open positions stay on the account under their stops and targets"]
    else:                                             # error
        head = _title(d)
        body = ([f"problem, retrying: {d.get('error')}"] if d.get("retrying")
                else [f"runner failed: {d.get('error')}"])
    return "\n".join([f"{head} — {body[0]}", *body[1:]])
