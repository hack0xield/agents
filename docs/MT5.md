# Per-user MT5 access — what it unlocks and what it costs

Notes from reviewing `../trading/mt5-mcp-server/` against spec §16–19 and §28.

---

## 1. Before anything: the write tools

The existing connector exposes 18 tools. Seven of them trade:

```text
✗ place_market_order   ✗ close_position   ✗ modify_position
✗ place_pending_order  ✗ close_all        ✗ modify_pending_order
                       ✗ cancel_order
```

There is a `read_only` config flag that makes them refuse — and it **defaults to
`false`**.

That is not the guarantee spec §10 asks for. §10 says the trade tools must not
exist, and the difference matters: a flag is one bad config merge, one forgotten
env var, or one copied example file away from an assistant that can trade a
customer's account. "It was set to true in staging" is not a defence anyone wants
to write up afterwards.

**Build a read-only façade.** A separate MCP server that imports only the eleven
read handlers, so the write handlers are never registered in the process the
agent can reach. Three independent layers, in order of reliability:

1. **Absence** — the façade never registers a write tool. Nothing to misconfigure.
2. **The flag** — `read_only: true` on the underlying connector as a backstop.
3. **The broker** — investor password, so MT5 itself rejects trading (§3.4).

Note the connector's config takes a plain `password` with no notion of which kind
it is. Onboarding must reject master passwords, and nothing should ever store one.

---

## 2. What the data actually unlocks

This is where the product's moat is (§28, §67 layer 3). Every one of these is
**deterministic — no LLM in the detection path** (§2). The model only explains
what code found.

| Signal | Needs | Source |
|---|---|---|
| Risk escalation after loss | position size, balance, prior outcome | `get_positions` + `get_deals` |
| Revenge re-entry | close time, re-entry time, symbol, direction, size | `get_deals` + `get_positions` |
| Overtrading | trades today vs personal median | `get_deals` |
| Loss chasing | size sequence across consecutive losses | `get_deals` |
| **Stop widening** | SL value **over time** | ⚠ not obtainable by polling current state |
| Prop-firm limits | daily P&L vs limit, drawdown vs max | `get_account` + `get_deals` |
| Session discipline | entry times vs stated preferred session | `get_deals` |

**Stop widening is the one that changes the architecture.** You cannot see a stop
being moved by asking what the stop is now — you need the previous value. That
forces periodic snapshots rather than on-demand reads, which is what spec §50's
`account_snapshots` is for, and it is the difference between an assistant that
answers questions and one that notices things.

Once snapshots exist, everything else gets easier: the event stream (§16) falls
out of diffing consecutive snapshots into `POSITION_OPENED`, `POSITION_MODIFIED`,
`POSITION_CLOSED`.

### The prop-firm angle is the strongest one

The target customer is buying challenges and failing them. From `get_account`
plus the challenge parameters, entirely deterministically:

> Your challenge has 2.3% daily loss capacity remaining.

> This position at 1.2% risk would put you within 0.4% of your daily limit.

No LLM, no prediction, no backtest needed — arithmetic against rules the trader
already agreed to. It is the cheapest high-value thing on this list and it maps
directly to why people churn.

---

## 3. Shared vs per-user — do not confuse them

The connector mixes two categories of tool, and they scale completely
differently (§15, §58):

| Per user | Shared across all users |
|---|---|
| `get_account` | `get_rates` |
| `get_positions` | `get_ticks` |
| `get_deals` | `get_symbol_info` |
| `get_orders_history` | `list_symbols` |
| `get_pending_orders` | `get_price` |

Market data must be fetched **once per instrument**, not once per user. Six
hundred users watching XAUUSD is one price feed and six hundred account
connections — never six hundred price feeds. Getting this wrong is the difference
between an infrastructure bill that scales with instruments and one that scales
with customers.

---

## 3b. Measured capacity — and the one open question

Measured on the installed terminal, 2026-08-20 (full data in
`experiments/RESULTS.md`):

| | |
|---|---|
| Cold start — launch + login + query | 3.0–3.5 s |
| Warm reconnect — terminal already up | 4–7 ms |
| terminal64.exe footprint | 262–300 MB RSS, 6.8% CPU |
| Concurrent terminals (memory-bound, 32 GB box) | ~65–70 |
| Disk per instance | 298 MB (1.1 GB with the price cache) |

The terminal survives `mt5.shutdown()`, so connection lifetime and terminal
lifetime are independent. A 3.4 s cold start also means pure on-demand is
viable for answering questions — it is only proactive monitoring that needs
something running.

### Open: can one terminal serve several accounts?

**Untested — needs a second credential, and it decides the architecture.**

If re-login is cheap and unpoliced, one terminal multiplexes many accounts and
the fleet is small. If it is slow or rate-limited, it is one terminal per
account and §19's local bridge becomes the scaling answer rather than a
footnote.

To settle it, create a second free MetaQuotes demo account and measure:

1. `initialize(login=A)` → `shutdown()` → `initialize(login=B)` against the
   same terminal path. Does it re-authenticate, and how long does it take?
2. Whether sustained A→B→A switching trips rate limiting or security flags.
   A prop firm will police this harder than a demo server, so testing against
   FTMO matters more than testing against MetaQuotes-Demo.

Until that is answered, size the fleet pessimistically at one terminal per
account and treat multiplexing as an optimisation, not a plan.

## 4. The infrastructure reality

`MetaTrader5` is a Windows-only package that talks to `terminal64.exe` over a
named pipe — this repo already runs it under Wine, per `../trading/CLAUDE.md`.
Each connected account needs a terminal instance holding a live session.

That means per-user MT5 is the product's real marginal cost, not tokens. Spec §35
already reflects this: charge separately for additional connected accounts,
because "account count creates infrastructure cost, while Telegram conversations
alone do not". LLM spend is ~$2.50/user/month; a persistent Windows terminal is
not.

Spec §18's fleet — account registry, connector scheduler, health states
(`CONNECTED`/`DISCONNECTED`/`AUTH_FAILED`/`STALE`/`RECONNECTING`) — is where this
goes at scale. How many terminals one worker sustains has to be load-tested, not
assumed.

§19's local bridge (a light EA on the trader's own PC, streaming to us) removes
both the hosting cost and the need to hold anyone's password. Worse onboarding,
and monitoring stops when their machine sleeps. Worth keeping in view as the
scaling answer, not the starting one.

---

## 4b. What is built

The read-only façade from §1 exists: `mt5_bridge/bridge.py`, running under the
Wine Python that shares the MT5 prefix, serving loopback HTTP on :8082.

It calls exactly three MT5 functions — `account_info`, `positions_get`,
`history_deals_get`. No trading entry point is imported anywhere in the file, so
no configuration can reach one.

### One account per request, never a default

The bridge originally read a single hardcoded login from the backtester's
`config.json` — an account that repo uses to *fetch bars*, not to represent a
trader. Every user of the assistant would have been shown that account's
balance and positions as their own.

That is the worst failure shape available here: not an error, but a confident
wrong answer about someone's money. Fixed by removing the default entirely.
Each request must carry `?ref=<credential_ref>`; a request without one is
refused. Refs resolve in `mt5_bridge/accounts.json` (gitignored, mode 600) and match
`trading_accounts.credential_ref` in Postgres — the database holds the ref,
only that file resolves it to a secret.

An entry gives a password inline, or references one that already exists:

```json
"founder-demo": {
  "login": 110119104,
  "server": "MetaQuotes-Demo",
  "password_from": {"file": "Z:\\…\\mt5-mcp-server\\config.json", "key": "password"}
}
```

The pointer form matters. Copying an existing credential into a second file
doubles the surface and guarantees drift the first time one is rotated — so the
founder account references the backtester's config rather than duplicating it.
Spec §47 wants these in a secrets manager; one file is the POC stand-in, and
one is the operative word.

The ref is a server-owned argument (`tools.USER_SCOPED_ARGS`), so a model that
names someone else's ref has it discarded. A user with no connected account
gets `NO_ACCOUNT`, which is a different answer from "no open positions".

`mcp_server/mt5_live.py` is the Linux-side client. **Live only — the fixtures
are deleted, not disabled.** Quietly serving stub data for a real account is
the worst failure available here, and keeping a plausible fake in the tree is
what makes it reachable. An unreachable bridge returns
`connection_state: DISCONNECTED` rather than an empty list, because "no
positions" and "cannot see your account" are different sentences.

Trade history is reconstructed from deals: MT5 records an entry deal and an
exit deal sharing a `position_id`, so they are paired into one row per
completed round trip. Reporting raw deals would show every position twice.

Live against the founder's demo account, the first thing it surfaced was
`access: MASTER_TRADING_ENABLED` — the §1 problem, confirmed in production data
rather than in theory.

## 4c. How an account gets connected — and what is missing

**Nothing populates the store automatically.** Today:

```bash
./scripts/pair.py "Name"                    # user + telegram binding
./scripts/connect-account.py --user <uuid> \
    --login 10534821 --server FTMO-Demo2 --nickname "FTMO 100K"
```

`connect-account.py` writes both halves — the credential into
`accounts.json`, and the `trading_accounts` row that maps a user to it — and
rolls the credential back out if the database write fails. Half a connection is
worse than none: an orphaned secret on disk with no row pointing at it is a
liability nobody remembers to clean up.

The bridge reloads the store when the file changes, so a new account does not
require a restart that would drop every live terminal session.

### This is an operator tool, not onboarding

Spec §3.4 is explicit: the trader types their MT5 credentials into an
authenticated HTTPS page, never into a chat, and never into someone else's
terminal. `connect-account.py` prompts on the operator's console — fine for
founder-alpha, wrong for a customer. The web form is the missing piece, and it
belongs with the website (§65 step 13).

### One JSON file does not scale to real users

`accounts.json` currently holds every connected account. At one user that is a
POC compromise. At fifty it is a single file whose disclosure is a total
breach of every customer's account credentials, sitting on the same host that
runs a Wine prefix and an HTTP server.

Spec §47 is unambiguous — credentials belong in a secrets manager, encrypted at
rest, never in a plain file. `credential_ref` exists precisely so that
substitution is a change to one function (`_resolve_secret`) rather than to
every call site. Make that change before onboarding anyone who is paying.

## 5. Build order

1. **Read-only façade** over the existing connector. Nothing else is safe to
   start until the write tools are unreachable.
2. **Snapshot poller** → `account_snapshots`, `trades`, `positions`, all carrying
   `user_id` from the first migration (§48).
3. **Event derivation** — diff snapshots into the §51 canonical events.
4. **Discipline rules** — deterministic, tested against replayed history (§60).
   Start with prop-firm limits: highest value, simplest maths.
5. **Swap the `mt5.*` fixtures** for real data. The tool contract in
   `mcp_server/server.py` already matches, so the agent side does not change.

Step 5 is last on purpose. Steps 2–4 are what make the assistant notice things;
step 5 only makes it answer questions about them.
