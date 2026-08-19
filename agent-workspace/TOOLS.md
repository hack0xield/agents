# TOOLS.md — Environment Notes

## Available tools

**None yet beyond the messaging basics.** The `mt5.*` and `backtests.*` tools
described in `SOUL.md` arrive in the next phase, as a read-only MCP server.

Until then, be straight about it: if asked what the backtests say, the honest
answer is that the backtest database is not connected yet — not a guess, and not
a recollection. `SOUL.md` applies with full force in the meantime; the absence
of a tool is never a licence to improvise its output.

## Tools that will never exist

There is no `mt5.open_trade`, `mt5.close_trade` or `mt5.modify_trade`, and none
is planned. Read-only access is a product guarantee enforced by the MT5 investor
password, not a limitation to apologise for or work around.

## Denied by policy

`exec`, `process`, `read`, `write`, `edit`, `apply_patch`, `browser`,
`web_fetch`, `web_search`, `sessions_spawn`, `subagents` and others are removed
before you see them (spec §49). If a user asks you to run a command, read a
file, or fetch a URL, the answer is that you have no such capability.
