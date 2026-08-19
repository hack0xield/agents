"""Attach to a specific MT5 install and hold the connection. READ-ONLY."""
import json, sys, time
import MetaTrader5 as mt5

CONFIG = r"Z:\home\epershyn\Documents\trading_assistant\trading\mt5-mcp-server\config.json"
path, hold = sys.argv[1], int(sys.argv[2])
cfg = json.load(open(CONFIG))
t0 = time.perf_counter()
ok = mt5.initialize(path=path, login=int(cfg["login"]), password=cfg["password"],
                    server=cfg["server"], timeout=60000)
init = round((time.perf_counter() - t0) * 1000)
if not ok:
    print(json.dumps({"path": path, "ok": False, "err": str(mt5.last_error()), "init_ms": init}), flush=True)
    sys.exit(1)
acc = mt5.account_info()
print(json.dumps({"path": path, "ok": True, "init_ms": init,
                  "login": acc.login if acc else None}), flush=True)
time.sleep(hold)          # hold the connection so both run concurrently
mt5.shutdown()
