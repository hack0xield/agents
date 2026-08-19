"""One MT5 connect/query/disconnect cycle, timed.

READ-ONLY. Calls account_info, positions_get, history_deals_get and nothing
else. No order function is imported or referenced anywhere in this file.

Runs under the Wine Python that shares the MT5 prefix:
    WINEPREFIX=~/.mt5 wine 'C:\\Python311\\python.exe' mt5_probe.py [cycles]
"""

import json
import sys
import time
from datetime import datetime, timedelta

import MetaTrader5 as mt5

CONFIG = r"Z:\home\epershyn\Documents\trading_assistant\trading\mt5-mcp-server\config.json"


def load():
    with open(CONFIG) as f:
        return json.load(f)


def one_cycle(cfg, want_detail=False):
    """initialize -> read -> shutdown. Returns timings in ms."""
    t = {}

    t0 = time.perf_counter()
    ok = mt5.initialize(
        path=cfg["mt5_path"],
        login=int(cfg["login"]),
        password=cfg["password"],
        server=cfg["server"],
        timeout=cfg.get("timeout", 60) * 1000,
    )
    t["init_ms"] = round((time.perf_counter() - t0) * 1000)
    if not ok:
        return {"ok": False, "error": mt5.last_error(), **t}

    t0 = time.perf_counter()
    acc = mt5.account_info()
    t["account_ms"] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    pos = mt5.positions_get()
    t["positions_ms"] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    deals = mt5.history_deals_get(datetime.now() - timedelta(days=7), datetime.now())
    t["history_ms"] = round((time.perf_counter() - t0) * 1000)

    detail = {}
    if want_detail and acc is not None:
        detail = {
            # The field that decides whether a credential is investor-grade.
            "trade_allowed": acc.trade_allowed,
            "trade_mode": acc.trade_mode,
            "login": acc.login,
            "server": acc.server,
            "currency": acc.currency,
            "leverage": acc.leverage,
            "positions": len(pos) if pos else 0,
            "deals_7d": len(deals) if deals else 0,
        }

    t0 = time.perf_counter()
    mt5.shutdown()
    t["shutdown_ms"] = round((time.perf_counter() - t0) * 1000)

    t["total_ms"] = sum(v for k, v in t.items() if k.endswith("_ms"))
    return {"ok": True, **t, **({"detail": detail} if detail else {})}


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cfg = load()
    out = []
    for i in range(cycles):
        r = one_cycle(cfg, want_detail=(i == 0))
        r["cycle"] = i + 1
        out.append(r)
        print(json.dumps(r), flush=True)
        if not r.get("ok"):
            break
        if i + 1 < cycles:
            time.sleep(2)   # don't hammer the broker
    print("RESULT " + json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
