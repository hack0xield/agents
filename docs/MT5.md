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
