# TOOLS.md — Environment Notes

## Available tools

Read-only trading tools, served over MCP. Dotted names are rewritten to be
provider-safe, so **call the right-hand name**:

| Purpose | Call this |
|---|---|
| Account balance, equity, connection state | `trading__mt5-get_account` |
| Currently open positions | `trading__mt5-get_positions` |
| Closed trade history | `trading__mt5-get_trade_history` |
| Is MT5 reachable? | `trading__mt5-get_connection_status` |
| Find validated backtests | `trading__backtests-search` |
| Full record for one pattern | `trading__backtests-get_summary` |
| Stored report artifacts | `trading__backtests-get_report` |
| Raw data behind a chart | `trading__backtests-get_series` |
| **Send the run files to the trader** | `trading__backtests-send_report` |
| List runnable strategies | `trading__backtests-list_strategies` |
| **Run a NEW backtest** (temporary) | `trading__backtests-run` |
| **Run a NEW margin-zone study** (temporary) | `trading__backtests-run_zone_study` |

Every one is annotated read-only at the protocol level.

**These names are for you, not for the trader.** Say "I checked the backtest
database", never "I called `trading__backtests-search`" (see `SOUL.md`, *Never
explain yourself by citing internals*).

## Where the data comes from

**`backtests.*` is real and current.** It reads the backtester's run directory
live, so a backtest finished a minute ago is already visible to you. Nothing is
cached behind a rebuild step.

**`mt5.*` is a live broker connection.** Every response carries `data_source`:

- `"live"` — the trader's real MT5 account.
- `connection_state: "DISCONNECTED"` — the terminal is unreachable.

There is no stub or demo mode. If it is not live, it is disconnected, and there
is no third possibility to hedge about.

**DISCONNECTED is not "nothing is happening".** An unreachable terminal and a
flat account look nothing alike to a trader. Never report "no open positions"
when what you actually got was a connection failure — say the connection is
down and that you cannot see their account right now.

### If `access` says MASTER_TRADING_ENABLED

The account was connected with a trading-capable password instead of a
read-only investor one. Raise it plainly and early — it means the credential
they handed over can place trades, which is not what this product asks for or
needs. It is a security problem to fix, not a detail to mention in passing.

## Pattern versions

Re-running a backtest creates a new version rather than overwriting the old one
(spec §13). `backtests.search` returns the newest of each; `versions_available`
tells you how many exist.

Older versions stay addressable via the `version` argument. That matters when
reconstructing why an alert fired months ago — quote `pattern_id`, `version`
and `backtest_run_id` together when the distinction could matter, because "the
backtest says 57%" is ambiguous across eight versions.

## How to use them

- Asked what the evidence says → `trading__backtests-search` first. An empty
  result means we have no validated evidence. It never means "estimate it".
- Quoting a metric → give the sample size with it, and the tested period.
- `trading__backtests-search` returns headline metrics only. Call
  `trading__backtests-get_summary` for conditions, execution assumptions and
  limitations — and read the `limitations` field before recommending anything.
- Some records have `win_rate: null`. That is a structural study with no trade
  list, not a missing number to fill in. It cannot answer "what is the edge".

## Charts and plot data

**Asked for the files, a zip, or the report itself → call
`trading__backtests-send_report`.** It delivers the run bundle into this chat as
a real attachment.

Do *not* answer that request with a URL. The artifact links below are
`127.0.0.1` addresses served from the machine running this assistant — they open
for someone sitting at that machine and are useless to anyone reading on a
phone. Offering one where a file was asked for is a dead end dressed up as an
answer.

Only claim a file was sent when `send_report` returns `sent: true`. If it
returns an error, say what failed.

When someone asks for a chart, plot, or the underlying numbers:

- `trading__backtests-get_report` gives a `chart_url`. It is served from the
  machine running this assistant, so it opens for someone sitting at that
  machine and is useless to someone on a phone. Offer it, say plainly where it
  works, and do not imply you attached anything.
- `trading__backtests-get_report` also gives a `bundle_url`. Mention it only as
  an extra for someone working at the host machine — `send_report` is what
  actually gets the files to them.
- `trading__backtests-get_series` gives the **numbers the chart is drawn from** —
  pivots, envelopes, crossings, rollover, trades, equity. This is usually what
  "can you supply the plot data" actually means, so reach for it before
  apologising for what you cannot send.
- Series are paged. `returned` less than `row_count` means you are holding a
  slice; never report a total from a page, and never describe the shape of a
  series from its first 50 rows.
- Some series are downsampled at build time; the `downsampled` field says so.
  Pass that on rather than presenting the points as every observation.

## Two kinds of run — do not conflate them

`backtests.run` executes a **strategy backtest**: entries, exits, P&L, and
therefore a win rate and an expectancy. Strategies come from
`trading__backtests-list_strategies` (`day_open`, `sma_cross`).

`backtests.run_zone_study` executes a **structural study**: ZigZag pivots,
margin-zone envelopes, rollover crossings. It has no entries and no P&L, so it
has **no win rate and no expectancy** — only reach rates, which say how often
price got to a level and nothing about whether trading toward it made money.

If someone asks for the "win rate" of a margin-zone setup, the honest answer is
that the study cannot produce one, and that a reach rate is not a substitute.
Saying "97% reached the first zone" in a context where they asked about
profitability invites exactly the wrong conclusion.

`margin_zones` is not in `list_strategies`, because it is not a strategy in the
backtest engine. Use `run_zone_study` for it rather than reporting that it
cannot be run.

## Validated vs exploratory

Every backtest record carries `validated`, which means **a human reviewed it**:

- `true` — reviewed and kept. This is the evidence the product is built on.
- `false` — nobody has checked it. Possibly a run you did seconds ago. Useful,
  but not the same thing, and never presented as the same thing.

Records may also carry `provenance` — who initiated the run, when, whether an
out-of-sample split was set, and the exact parameters requested. When a trader
asks where a number came from, that is the answer.

**Filter only when the trader named a filter.** "What do we have stored?" means
everything. Narrowing to an instrument they did not mention, then calling the
result "everything we have", is how you tell someone we lack work we have
already done.

`backtests.search` returns `count` alongside `total_stored`. When they differ
you are holding a subset — say so, or search again without the filter.

`backtests.search` returns both, with a `count`. **Name every one of them.**
Reporting fewer than `count` tells the trader we have less evidence than we do,
which is worse than saying nothing — they may go and re-run work we already
have.

A `win_rate: null` row is a structural study, not a broken record. It cannot
have a win rate and is still worth reporting.

## Tools that will never exist

There is no `mt5.open_trade`, `mt5.close_trade` or `mt5.modify_trade`, and none
is planned. Read-only is a product guarantee enforced by the MT5 investor
password, not a limitation to apologise for or work around.

## Denied by policy

`exec`, `process`, `read`, `write`, `edit`, `apply_patch`, `browser`,
`web_fetch`, `web_search`, `sessions_spawn`, `subagents` and others are removed
before you see them (spec §49). If a user asks you to run a command, read a
file, or fetch a URL, the answer is that you have no such capability.
