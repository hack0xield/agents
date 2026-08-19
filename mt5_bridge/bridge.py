"""Read-only HTTP bridge to a live MT5 terminal.

The MetaTrader5 package is Windows-only — it talks to terminal64.exe over a
named pipe — so it cannot be imported from the Linux interpreter that runs the
MCP server. This process runs under the Wine Python that shares the MT5 prefix
and exposes account state over loopback HTTP; the MCP server calls it.

THIS IS THE READ-ONLY FACADE (docs/MT5.md §1).

Only three MT5 functions are ever called: account_info, positions_get and
history_deals_get. `order_send` and every other trading entry point is absent
from this file — not disabled by a flag, absent. A flag defaults wrong once and
then an assistant is trading someone's account; an unimported function cannot
be called by any configuration mistake.

Do not add a write endpoint here. If one is ever needed it belongs in a
different process with a different trust story.

Run:  ./scripts/mt5-bridge.sh
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import MetaTrader5 as mt5

HOST, PORT = "127.0.0.1", 8082
CONFIG = r"Z:\home\epershyn\Documents\trading_assistant\trading\mt5-mcp-server\config.json"

POSITION_TYPE = {0: "BUY", 1: "SELL"}
DEAL_REASON = {
    0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT",
    4: "STOP_LOSS", 5: "TAKE_PROFIT", 6: "STOP_OUT",
    7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT",
}

_cfg = None


def cfg():
    global _cfg
    if _cfg is None:
        with open(CONFIG) as f:
            _cfg = json.load(f)
    return _cfg


def ensure_connected() -> bool:
    """Attach to the terminal, launching it if needed.

    Cheap when the terminal is already up (measured 4-7 ms); ~3.4 s cold.
    Safe to call per request.
    """
    acc = mt5.account_info()
    if acc is not None and acc.login != 0:
        return True
    c = cfg()
    return bool(mt5.initialize(
        path=c["mt5_path"], login=int(c["login"]),
        password=c["password"], server=c["server"],
        timeout=int(c.get("timeout", 60)) * 1000,
    ))


def iso(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def account() -> dict:
    """Normalized account state (spec §16) — broker quirks flattened here so the
    agent never sees an MT5-specific field name."""
    a = mt5.account_info()
    if a is None:
        return {"connection_state": "DISCONNECTED", "error": str(mt5.last_error())}
    pos = mt5.positions_get() or ()
    return {
        "account_id": f"mt5_{a.login}",
        "nickname": a.name or f"{a.server} {a.login}",
        "broker": a.company,
        "server": a.server,
        "login": a.login,
        "currency": a.currency,
        "leverage": a.leverage,
        # trade_allowed is how an investor password is distinguished from a
        # master one. Surfaced rather than hidden: if this says trading is
        # enabled, onboarding accepted the wrong credential (docs/MT5.md §1).
        "access": "investor_read_only" if not a.trade_allowed else "MASTER_TRADING_ENABLED",
        "trade_allowed": a.trade_allowed,
        "balance": round(a.balance, 2),
        "equity": round(a.equity, 2),
        "margin": round(a.margin, 2),
        "free_margin": round(a.margin_free, 2),
        "margin_level": round(a.margin_level, 2) if a.margin_level else None,
        "open_positions": len(pos),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "connection_state": "CONNECTED",
    }


def positions() -> list:
    out = []
    for p in mt5.positions_get() or ():
        out.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "direction": POSITION_TYPE.get(p.type, str(p.type)),
            "volume": p.volume,
            "open_time": iso(p.time),
            "open_price": p.price_open,
            "current_price": p.price_current,
            "stop_loss": p.sl or None,
            "take_profit": p.tp or None,
            "profit": round(p.profit, 2),
            "swap": round(p.swap, 2),
            "comment": p.comment or None,
        })
    return out


def history(days: int = 30, symbol: str | None = None) -> list:
    """Closed round-trip trades, reconstructed from deals.

    MT5 records deals, not trades: an entry deal and an exit deal share a
    position_id. Reporting raw deals would double-count and show every position
    twice, so they are paired here — one row per completed position, which is
    what a trader means by "my last trades".

    Positions still open have no exit deal and are excluded; they belong to
    /positions.
    """
    now = datetime.now()
    deals = mt5.history_deals_get(now - timedelta(days=days), now) or ()

    groups: dict[int, dict] = {}
    for d in deals:
        if symbol and d.symbol.upper() != symbol.upper():
            continue
        g = groups.setdefault(d.position_id, {"in": None, "out": [], "sym": d.symbol})
        if d.entry == mt5.DEAL_ENTRY_IN:
            g["in"] = d
        elif d.entry == mt5.DEAL_ENTRY_OUT:
            g["out"].append(d)

    out = []
    for pid, g in groups.items():
        entry, exits = g["in"], g["out"]
        if entry is None or not exits:
            continue                      # still open, or partial history window
        last = max(exits, key=lambda d: d.time)
        profit = sum(d.profit for d in exits)
        swap = sum(d.swap for d in exits) + entry.swap
        commission = sum(d.commission for d in exits) + entry.commission
        out.append({
            "ticket": pid,
            "symbol": g["sym"],
            "direction": POSITION_TYPE.get(entry.type, str(entry.type)),
            "volume": entry.volume,
            "open_time": iso(entry.time),
            "open_price": entry.price,
            "close_time": iso(last.time),
            "close_price": last.price,
            "close_reason": DEAL_REASON.get(last.reason, str(last.reason)),
            "profit": round(profit, 2),
            "swap": round(swap, 2),
            "commission": round(commission, 2),
        })
    out.sort(key=lambda t: t["close_time"])
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/health":
                ok = ensure_connected()
                return self._send({"ok": ok, "connection_state": "CONNECTED" if ok else "DISCONNECTED"})
            if not ensure_connected():
                return self._send({"error": "mt5 not connected",
                                   "detail": str(mt5.last_error()),
                                   "connection_state": "DISCONNECTED"}, 503)
            if u.path == "/account":
                return self._send(account())
            if u.path == "/positions":
                return self._send(positions())
            if u.path == "/history":
                days = int(q.get("days", ["30"])[0])
                sym = q.get("symbol", [None])[0]
                return self._send(history(days=days, symbol=sym))
            self._send({"error": "not found"}, 404)
        except Exception as e:                      # never take the bridge down
            self._send({"error": type(e).__name__, "detail": str(e)}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] %s\n" % (fmt % args))


if __name__ == "__main__":
    print(f"[bridge] read-only MT5 bridge on http://{HOST}:{PORT}", flush=True)
    print(f"[bridge] connecting…", flush=True)
    t0 = time.perf_counter()
    print(f"[bridge] connected={ensure_connected()} in {(time.perf_counter()-t0)*1000:.0f} ms", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
