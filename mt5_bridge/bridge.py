"""A small HTTP server that reads one live MT5 account.

WHAT IT IS
    A long-running process that connects to the MetaTrader 5 terminal and
    answers three questions over loopback HTTP on 127.0.0.1:8082:

        GET /health      is the terminal reachable?
        GET /account     balance, equity, margin, connection state
        GET /positions   currently open positions
        GET /history     closed trades  (?days=30&symbol=XAUUSD)

    Nothing else talks to MT5. The MCP server (mcp_server/mt5_live.py) calls
    these endpoints, and the `mt5.*` tools the assistant sees are thin wrappers
    around them.

WHY IT IS A SEPARATE PROCESS
    The MetaTrader5 package is Windows-only — it talks to terminal64.exe over a
    named pipe — so it cannot be imported by the Linux interpreter that runs
    everything else. This file runs under the Wine Python that shares the MT5
    prefix (~/.mt5). That is the only reason it exists as its own service.

WHICH ACCOUNT
    Every data request must name one, with ?ref=<credential_ref>. There is no
    default and no fallback: a request without a ref is refused rather than
    served from whichever account happens to be connected.

    That matters because the alternative is the worst bug this system can have.
    The bridge originally read one hardcoded login — the backtester's
    data-fetch account — and would have answered /positions with that account's
    trades for every user who asked, labelled as their own. A wrong answer that
    looks right is worse than an error.

    Refs map to credentials in accounts.json (gitignored, mode 600), and match
    trading_accounts.credential_ref in Postgres. The database holds the ref;
    only this file holds a secret.

    Switching accounts re-initialises the terminal. Measured: ~3.5s cold, 4-7ms
    when already connected to that account. Whether one terminal can sustain
    many accounts by re-login is still untested — see docs/MT5.md §3b.

    Use an INVESTOR (read-only) password. The bridge reports access as
    MASTER_TRADING_ENABLED when account_info says trade_allowed, so a wrong
    credential is visible rather than silent (spec §3.4).

IT CANNOT TRADE
    Only three MT5 functions are called: account_info, positions_get,
    history_deals_get. `order_send` and every other trading entry point is
    absent from this file — not disabled by a flag, absent. A flag defaults
    wrong once and then an assistant is trading someone's account; an
    unimported function cannot be called by any configuration mistake.

    Do not add a write endpoint here. If one is ever needed it belongs in a
    different process with a different trust story.

RUN
    ./scripts/mt5-bridge.sh

    Takes ~3.5s if the terminal is not already running (it launches it), or a
    few milliseconds if it is. The terminal keeps running after this process
    exits.
"""

import json
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import MetaTrader5 as mt5

HOST, PORT = "127.0.0.1", 8082

# Wine maps the Linux root at Z:. This file sits next to bridge.py.
ACCOUNTS = str(Path(__file__).resolve().parent / "accounts.json")

POSITION_TYPE = {0: "BUY", 1: "SELL"}
DEAL_REASON = {
    0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT",
    4: "STOP_LOSS", 5: "TAKE_PROFIT", 6: "STOP_OUT",
    7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT",
}

_accounts: dict | None = None
_connected_ref: str | None = None


def accounts() -> dict:
    global _accounts
    if _accounts is None:
        with open(ACCOUNTS) as f:
            _accounts = {k: v for k, v in json.load(f).items()
                         if not k.startswith("_")}
    return _accounts


def ensure_connected(ref: str) -> tuple[bool, str]:
    """Connect the terminal to the account named by `ref`.

    Returns (ok, detail). Re-initialises when the terminal is on a different
    account, which is what makes one terminal serve several — cheap when
    already on the right one (measured 4-7 ms), ~3.5 s otherwise.
    """
    global _connected_ref

    cred = accounts().get(ref)
    if cred is None:
        return False, f"unknown credential ref: {ref}"

    if _connected_ref == ref:
        acc = mt5.account_info()
        if acc is not None and int(acc.login) == int(cred["login"]):
            return True, "already connected"

    mt5.shutdown()
    ok = mt5.initialize(
        path=cred["mt5_path"], login=int(cred["login"]),
        password=cred["password"], server=cred["server"],
        timeout=int(cred.get("timeout", 60)) * 1000,
    )
    if not ok:
        _connected_ref = None
        return False, f"initialize failed: {mt5.last_error()}"

    acc = mt5.account_info()
    if acc is None or int(acc.login) != int(cred["login"]):
        # Connected to something other than what was asked for. Serving it
        # would be exactly the wrong-account failure this design exists to
        # prevent, so refuse instead.
        _connected_ref = None
        return False, "terminal connected to a different account than requested"

    _connected_ref = ref
    return True, "connected"


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
                # Liveness only; says nothing about any particular account.
                return self._send({"ok": True, "refs": sorted(accounts()),
                                   "connected_ref": _connected_ref})
            if u.path == "/accounts":
                return self._send({"refs": sorted(accounts())})

            ref = (q.get("ref") or [None])[0]
            if not ref:
                # No default account, deliberately. Serving whichever account
                # happens to be connected is how one user sees another's
                # positions.
                return self._send({"error": "missing ref",
                                   "detail": "every data request must name ?ref=<credential_ref>",
                                   "connection_state": "DISCONNECTED"}, 400)

            ok, detail = ensure_connected(ref)
            if not ok:
                return self._send({"error": "mt5 not connected", "detail": detail,
                                   "ref": ref, "connection_state": "DISCONNECTED"}, 503)
            if u.path == "/account":
                return self._send({**account(), "credential_ref": ref})
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
    try:
        refs = sorted(accounts())
    except OSError as e:
        print(f"[bridge] cannot read {ACCOUNTS}: {e}", flush=True)
        print("[bridge] copy accounts.example.json to accounts.json", flush=True)
        raise SystemExit(1)
    print(f"[bridge] {len(refs)} account(s): {', '.join(refs)}", flush=True)
    print("[bridge] no default account — every request must pass ?ref=", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
