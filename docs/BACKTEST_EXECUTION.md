# TEMPORARY: the assistant can run backtests

**Status:** active for the POC. **Decision:** founder, 2026-08-20.
**The spec is unchanged and still describes the intended product.**

---

## What this deviates from

- **§33** — "It must **never** initiate a new backtest."
- **§9.2** — "It must never generate plausible statistics."

Both are implemented in `agent-workspace/SOUL.md`, which is amended for the POC.
`spec.txt` is not.

## Why the spec says otherwise

Three failure modes, all still real:

**Overfitting on demand.** A run takes 0.64s. "Try 2%… now 3%… now 1.5%…
London only?" is p-hacking with a conversational interface, and there is no
friction to slow it down. The twentieth variation that finally looks good is
noise, produced helpfully.

**Evidence-hierarchy collapse.** §9.1 Level A is *verified* backtest evidence.
A run executed four seconds ago, unreviewed, whole-period in-sample, is not
that — but in chat it looks identical unless something forces the distinction.

**Product identity.** §63: the assistant must not become "a signal-selling bot
with better prose". On-demand statistics generation is a different product with
a different liability profile.

## What contains it

The capability is unrestricted; the *bookkeeping* is what keeps this
reversible.

| | Validated | Ad-hoc |
|---|---|---|
| Directory | `trading/runs/` | `trading/runs-adhoc/` |
| `validated` | `true` | `false` |
| Versioning | own sequence | own sequence |
| Citable as Level A | yes | no |

Ad-hoc runs are versioned in their own sequence, so an exploratory run can
never become "v9" of a reviewed pattern and inherit its standing.

`SOUL.md` requires the agent to say a result is freshly computed and
unreviewed, and to name the fitting problem when asked to sweep parameters
rather than silently obliging.

`backtests.run` reports `out_of_sample: true` only when `end` was set —
otherwise the whole period is in-sample and the number is optimistic.

## Reverting

1. Delete the `backtests.run` and `backtests.list_strategies` tools from
   `mcp_server/server.py`.
2. Restore the "You never run new backtests" paragraph in `SOUL.md`.
3. Delete `trading/runs-adhoc/`.

Nothing else depends on it. `runs.py` keeps the `validated` flag either way,
which is useful regardless.

## If this becomes permanent

The design that keeps §33 intact while delivering most of the value is the
research queue: an untested question is logged as a request rather than
refused, a human reviews and runs it, and the result is published into the
validated KB. That turns every "we don't have that" into demand signal about
which backtests to build next. Worth revisiting before this ships to anyone
who is paying.
