"""HTTP server exposing one live MT5 account, read-only, on 127.0.0.1:8082.

    GET /health                  liveness, and which refs are configured
    GET /accounts                configured credential refs
    GET /account?ref=X           balance, equity, margin, connection state
    GET /positions?ref=X         open positions
    GET /history?ref=X&days=30   closed trades

Separate process because the MetaTrader5 package is Windows-only; this runs
under the Wine Python sharing the MT5 prefix. mcp_server/mt5_live.py is the
only caller.

`ref` is required and has no default: serving whichever account happens to be
connected is how one user reads another's positions. Refs resolve to
credentials in accounts.json (gitignored) and match
trading_accounts.credential_ref in Postgres.

Only account_info, positions_get and history_deals_get are called. No trading
entry point is imported, so no configuration mistake can reach one. Do not add
a write endpoint here.

Details, including the untested multi-account question: docs/MT5.md
"""

import json
import os
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
_accounts_mtime: float | None = None
_connected_ref: str | None = None


def _resolve_secret(entry: dict) -> dict:
    """Fill in a password held somewhere else.

    An entry may carry `password` inline, or point at an existing file with
    `password_from: {file, key}`. The pointer form exists so a credential that
    already lives somewhere is referenced rather than copied — two copies of a
    secret is twice the surface, and they drift the moment one is rotated.
    """
    if entry.get("password"):
        return entry
    src = entry.get("password_from")
    if not src:
        return entry
    try:
        with open(src["file"]) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot read password_from {src.get('file')}: {e}")
    value = data.get(src.get("key", "password"))
    if not value:
        raise RuntimeError(f"no '{src.get('key','password')}' in {src['file']}")
    return {**entry, "password": value}


def accounts() -> dict:
    """Credential store, reloaded when the file changes.

    Cached by mtime rather than forever: connecting a new account must not
    require restarting the bridge and dropping every live terminal session.
    """
    global _accounts, _accounts_mtime
    try:
        mtime = os.stat(ACCOUNTS).st_mtime
    except OSError:
        mtime = None
    if _accounts is None or mtime != _accounts_mtime:
        with open(ACCOUNTS) as f:
            raw = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        _accounts = {k: _resolve_secret(v) for k, v in raw.items()}
        _accounts_mtime = mtime
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
    print("[bridge] ctrl-c to stop", flush=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True          # do not block exit on open requests
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        mt5.shutdown()                    # release the terminal, leave it running
        print("[bridge] stopped", flush=True)
