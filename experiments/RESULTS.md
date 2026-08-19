# MT5 capacity measurements

Measured 2026-08-20 on this machine: 12 cores, 32 GB RAM, MT5 build via Wine
(`~/.mt5`), MetaTrader5 python package 5.0.5735, account on `MetaQuotes-Demo`.

All probes are read-only — `account_info`, `positions_get`, `history_deals_get`.
No order function is referenced in `mt5_probe.py` or `mt5_multi.py`.

## Timings

| Operation | Time |
|---|---|
| Cold start — launch terminal + login + query | **3.0–3.5 s** |
| Warm reconnect — terminal already running | **4–7 ms** |
| `account_info` / `positions_get` / `history_deals_get` | <1 ms each |

The terminal **survives `mt5.shutdown()`**. The client disconnects; the process
stays up. So "connection" and "terminal" are separate lifecycles, and a warm
terminal can be reattached to for the price of a few milliseconds.

## Footprint

| | RSS |
|---|---|
| terminal64.exe | 262–300 MB |
| python client process | 60 MB |
| wineserver (shared, once) | 23 MB |
| install on disk, minimal copy | 298 MB |
| install on disk, with `Bases` price cache | 1.1 GB |

`Bases/` is 698 MB of regenerable price history — excluded from the second
instance with no ill effect.

## Concurrency

Two terminals ran simultaneously from separate install directories
(`C:\Program Files\MetaTrader 5` and `C:\mt5-inst2`), each with its own client,
both connecting successfully:

```text
pid=336840 RSS=300 MB  C:\Program Files\MetaTrader 5\terminal64.exe   init 3304 ms
pid=336922 RSS=262 MB  C:\mt5-inst2\terminal64.exe                    init 3007 ms
```

Separate install directories are required — the Python client attaches to a
terminal by `path`, so two accounts need two paths.

**Extrapolated ceiling on this box:** ~19 GB available / ~280 MB ≈ **65–70
concurrent terminals**, memory-bound. CPU is not the constraint (6.8% each
against 12 cores ≈ 175). Disk at 298 MB per instance ≈ 190 instances in 58 GB.

## Not measured — needs a second account

Whether **one terminal can serve several accounts by re-login** is the question
that decides the whole architecture, and it cannot be answered with one
credential. To test it, create a second free MetaQuotes demo account and
measure:

1. `initialize(login=A)` → `shutdown()` → `initialize(login=B)` on the same
   terminal path. Does it re-authenticate, and how long does it take?
2. Whether repeated A→B→A switching trips broker rate limiting or security
   flags. This is the risk that would invalidate multiplexing entirely, and a
   prop firm is likelier to police it than a demo server.

## Credential finding

`account_info().trade_allowed` is **`true`** on the configured account, and
`config.json` has `read_only: false`. The stored credential is a master
password, not an investor password — so the connector could place orders on it
right now. It is a demo account, so nothing is at risk, but it confirms that
nothing in the current setup verifies credential type, and `trade_allowed` is
exactly the field an onboarding gate should reject on.
