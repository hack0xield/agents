# TOOLS.md — Environment Notes

## Available tools

Read-only trading tools, served over MCP. OpenClaw rewrites dotted names to be
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

You cannot send or attach files. What you *can* do is hand over a link to one,
and there is a link for every stored artifact — so "I have no file access" is
only half the answer, and on its own it is a dead end for the user. Reach for
the URLs below before saying no.

When someone asks for a chart, plot, or the run files:

- `trading__backtests-get_report` gives a `chart_url`. It is served from the
  machine running this assistant, so it opens for someone sitting at that
  machine and is useless to someone on a phone. Offer it, say plainly where it
  works, and do not imply you attached anything.
- `trading__backtests-get_report` also gives a `bundle_url`: a zip of every
  file in the run — chart, summary and all CSVs. That is the answer when
  someone asks for "the files" or "a zip". You still cannot attach it; you hand
  over the link.
- `trading__backtests-get_series` gives the **numbers the chart is drawn from** —
  pivots, envelopes, crossings, rollover, trades, equity. This is usually what
  "can you supply the plot data" actually means, so reach for it before
  apologising for what you cannot send.
- Series are paged. `returned` less than `row_count` means you are holding a
  slice; never report a total from a page, and never describe the shape of a
  series from its first 50 rows.
- Some series are downsampled at build time; the `downsampled` field says so.
  Pass that on rather than presenting the points as every observation.

## Tools that will never exist

There is no `mt5.open_trade`, `mt5.close_trade` or `mt5.modify_trade`, and none
is planned. Read-only is a product guarantee enforced by the MT5 investor
password, not a limitation to apologise for or work around.

`backtests.*` retrieves stored results. Nothing here can run a new backtest.

## Denied by policy

`exec`, `process`, `read`, `write`, `edit`, `apply_patch`, `browser`,
`web_fetch`, `web_search`, `sessions_spawn`, `subagents` and others are removed
before you see them (spec §49). If a user asks you to run a command, read a
file, or fetch a URL, the answer is that you have no such capability.
