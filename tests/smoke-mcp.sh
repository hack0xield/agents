#!/usr/bin/env bash
# Call every tool against the RUNNING server and fail on any error.
#
# Exists because of a real escape: `runs = Path(...)` in server.py's __main__
# block shadowed the imported `runs` module, turning every backtests.* call
# into an AttributeError. In-process tests import server without executing
# __main__, so they all passed — only the live server was broken, and it
# surfaced as the agent telling a user "the backtest database is throwing an
# error". Import-level testing cannot catch a fault in the entrypoint.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! curl -s -o /dev/null --max-time 3 http://127.0.0.1:8081/mcp; then
  echo "error: no MCP server on :8081. Start it with ./scripts/mcp-server.sh" >&2
  exit 1
fi

exec "$REPO/.venv/bin/python" - <<'PY'
import asyncio, sys
sys.path.insert(0, 'mcp_server')
import runs as runs_mod
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

CALLS = [
    ("mt5.get_connection_status", {}),
    ("mt5.get_account", {}),
    ("mt5.get_positions", {}),
    ("mt5.get_trade_history", {"limit": 2}),
    ("backtests.search", {}),
    ("backtests.search", {"instrument": "XAUUSD"}),
    ("backtests.get_summary", {"pattern_id": "DAY_OPEN_XAUUSD_M15"}),
    ("backtests.get_report", {"pattern_id": "ZONES_EURUSD_H4_6E_DEV2PCT"}),
    ("backtests.get_series", {"pattern_id": "ZONES_EURUSD_H4_6E_DEV2PCT",
                              "series": "pivots", "limit": 2}),
    # Absence must be a clean empty answer, not an exception.
    ("backtests.search", {"instrument": "GBPUSD"}),
    ("backtests.get_summary", {"pattern_id": "DOES_NOT_EXIST"}),
    # Unknown pattern: exercises send_report's guard without sending a file.
    # Delivering to a real chat on every smoke run would be rude.
    ("backtests.send_report", {"pattern_id": "DOES_NOT_EXIST"}),
    ("backtests.list_strategies", {}),
]

# Ad-hoc runs live in a different directory from validated ones. get_report and
# send_report resolved against the validated root only, so anything the agent
# had just run was unreachable — it could produce a study and then not hand it
# over. Discovered in live use; covered here so it cannot come back.
ADHOC_CHECKS = [
    ("backtests.get_report", "found"),
]

_TAIL = [

]

async def main() -> int:
    failed = 0
    async with streamable_http_client("http://127.0.0.1:8081/mcp") as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            expected = {"mt5.get_account", "mt5.get_positions", "mt5.get_trade_history",
                        "mt5.get_connection_status",
                        "backtests.search", "backtests.get_summary",
                        "backtests.get_report", "backtests.get_series",
                        "backtests.send_report", "backtests.run",
                        "backtests.list_strategies", "backtests.run_zone_study",
                        "backtests.fetch_data"}
            if names != expected:
                print(f"  FAIL tool list: missing={expected - names} extra={names - expected}")
                failed += 1
            else:
                print(f"  ok   {len(names)} tools registered")

            for tool, args in CALLS:
                res = await s.call_tool(tool, args)
                txt = (getattr(res.content[0], "text", "") if res.content else "") or ""
                if res.is_error:
                    print(f"  FAIL {tool} {args}\n       {txt[:200]}")
                    failed += 1
                else:
                    print(f"  ok   {tool} {args}")
            # Ad-hoc runs live in a different directory from validated ones.
            # get_report and send_report resolved against the validated root
            # only, so a study the agent had just run was unreachable — it
            # could produce a result and then not hand it over. Found in live
            # use; asserted here so it cannot come back.
            adhoc = [r for r in runs_mod.all_patterns() if not r["validated"]]
            if adhoc:
                res = await s.call_tool("backtests.get_report",
                                        {"pattern_id": adhoc[-1]["pattern_id"]})
                txt = (getattr(res.content[0], "text", "") if res.content else "") or ""
                if res.is_error or '"found": true' not in txt.lower():
                    print(f"  FAIL ad-hoc run not reachable via get_report: "
                          f"{adhoc[-1]['pattern_id']}")
                    failed += 1
                else:
                    print("  ok   ad-hoc run reachable via get_report")
            else:
                print("  --   no ad-hoc runs present, skipping reachability check")

    print("\nFAILED" if failed else "\nAll green")
    return 1 if failed else 0

sys.exit(asyncio.run(main()))
PY
