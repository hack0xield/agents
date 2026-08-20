# USER.md — Trader Profile

Structured facts about the trader. Corresponds to spec §23, "Memory B".

Until the Product Core owns this (it belongs in Postgres behind typed tools, not
in prose — spec §27), it is maintained by hand in the repository and installed
with the rest of the workspace. The agent cannot edit it: filesystem tools are
denied.

- **Name:** Eduard
- **What to call them:** Eduard
- **Timezone:** Europe/Kyiv (UTC+3)

## Trading profile

_**Placeholder values.** Set by the assistant, not by the trader, so the
discipline rules have something to measure against on a demo account that is
not used for real trading. They are conventional prop-challenge defaults, not
this trader's actual limits. Replace them before any of this is applied to a
funded account — a rule the trader did not choose is not a rule they agreed to,
and flagging against it is noise._

- **Preferred instruments:** XAUUSD, EURUSD
- **Normal risk per trade:** 0.5% of balance
- **Maximum risk per trade:** 1.0% of balance
- **Maximum daily loss:** 2.0% of balance
- **Preferred session:** London / New York overlap
- **Typical trades per day:** 3
- **Account type:** demo (MetaQuotes-Demo, not traded)

## Rules the trader has asked to be held to

_Each entry licenses the assistant to raise the behaviour. Nothing is flagged
that is not listed — the assistant enforces the trader's rules, not its own
opinions about trading. These are placeholders alongside the limits above._

- Flag any position sized above 1.0% risk.
- Flag a re-entry on the same instrument and direction within 15 minutes of a
  stop-out, when the new position is larger than the one that was stopped.
- Flag the 4th trade of a day, and every trade after it.
- Flag a stop-loss moved further from entry after a position is open.
- Warn when cumulative loss for the day reaches 1.5%, before the 2.0% limit.

## Communication preferences

- **Style:** concise
- **Proactive alerts:** not yet implemented
