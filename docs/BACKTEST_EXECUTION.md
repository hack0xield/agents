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

The capability is unrestricted. What keeps it reversible is knowing which runs
were reviewed — and that is recorded **in the run**, not inferred from where it
sits.

### Provenance, not location

A `summary.json` produced by a human designing an experiment and one produced
by a chat message are byte-identical in structure. Nothing in the artifact says
which is which. So `backtests.run` writes `provenance.json` alongside it:

```json
{
  "initiated_by": "assistant",
  "initiated_at": "2026-08-19T23:11:15+00:00",
  "reviewed": false,
  "out_of_sample": true,
  "requested": { "strategy": "day_open", "params": {"stop_pct": 2.5}, ... }
}
```

`validated` derives from `reviewed` where provenance exists. Setting
`reviewed: true` promotes a run **without moving any files** — verified. That
matters because the useful case is real: an ad-hoc run that turns out to be
worth keeping should be promotable, and a careless run sitting in `runs/`
should not be blessed by its path.

### The directories

`trading/runs/` and `trading/runs-adhoc/` are **organisation, not the
guarantee**. Two honest reasons to keep them:

- Volume. Seven ad-hoc runs appeared during one afternoon of testing. In
  `runs/` they would be noise in the asset that matters.
- Runs made before provenance existed, and anything produced directly by the
  backtester CLI, have no `provenance.json`. For those the directory is the
  only signal available, so it remains the fallback.

They are **not** what stops an exploratory run becoming "v9" of a reviewed
pattern — the `--label adhoc` suffix already gives it a distinct pattern id
(`DAY_OPEN_XAUUSD_M15_ADHOC`), so version sequences cannot merge regardless.
An earlier draft of this document claimed otherwise.

Nor was overwriting ever a risk: every run directory carries a timestamp, so
collision is impossible by construction.

## Two runners, two shapes of result

`margin_zones` is not a registered strategy — `strategies/__init__.py` imports
only `day_open` and `sma_cross`, so `run_backtest.py --strategy margin_zones`
does not work. Zone studies come from `scripts/plot_zones.py`, a separate entry
point, which is why there are two tools:

| | `backtests.run` | `backtests.run_zone_study` |
|---|---|---|
| Runner | `run_backtest.py` | `plot_zones.py` |
| Produces | entries, exits, P&L | pivots, envelopes, crossings |
| Win rate / expectancy | yes | **no — cannot** |
| Headline number | win rate | reach rate |

Keeping them as separate tools rather than one dispatching tool is deliberate.
A reach rate and a win rate are both percentages in the nineties, and "97%
reached the first zone" reads like a 97% win rate to anyone who asked about
profitability. Two tools with two vocabularies make that conflation harder to
make by accident.

## Reverting

1. Delete `backtests.run`, `backtests.run_zone_study` and
   `backtests.list_strategies` from `mcp_server/server.py`.
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
