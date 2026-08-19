# TOOLS.md — Environment Notes

## Available tools

Read-only trading tools, served over MCP. OpenClaw rewrites dotted names to be
provider-safe, so **call the right-hand name**:

| Purpose | Call this |
|---|---|
| Account balance, equity, connection state | `trading__mt5-get_account` |
| Currently open positions | `trading__mt5-get_positions` |
| Closed trade history | `trading__mt5-get_trade_history` |
| Find validated backtests | `trading__backtests-search` |
| Full record for one pattern | `trading__backtests-get_summary` |
| Stored report artifacts | `trading__backtests-get_report` |

Every one is annotated read-only at the protocol level.

## This is stub data

The tools are backed by fixtures, not a live MT5 terminal. The numbers are real
— derived from actual backtest runs — but the account is not connected to a
broker and the history is a replayed backtest, not the trader's own trades.

**Say so when it matters.** If asked about "my trades", make clear you are
looking at POC fixture data. Never let stub data be mistaken for the trader's
live account.

## How to use them

- Asked what the evidence says → `trading__backtests-search` first. An empty
  result means we have no validated evidence. It never means "estimate it".
- Quoting a metric → give the sample size with it, and the tested period.
- `trading__backtests-search` returns headline metrics only. Call
  `trading__backtests-get_summary` for conditions, execution assumptions and
  limitations — and read the `limitations` field before recommending anything.
- Some records have `win_rate: null`. That is a structural study with no trade
  list, not a missing number to fill in. It cannot answer "what is the edge".

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
