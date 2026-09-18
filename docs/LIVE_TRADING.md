# PLAN: the assistant runs the live trader

**Status:** Phases 1–3 built and deployed to the server (2026-09-15), Algo
Trading enabled there, and `mz50` live on the shared demo account since
2026-09-15 21:02 UTC. Fixes from its first days (*First days live*) are built,
not yet deployed. Margin data is still stale.
**Decisions:** founder, 2026-09-15.
**The spec is unchanged and still describes the intended product.**

The trading repo can now trade a run config on the MT5 account
(`scripts/run-live.sh --config configs/strategies/mz50.yaml`, commit `d47a987`
on `margin-zones-streaming`). This plan makes the Telegram assistant able to
start it, stop it and report on it, keeps it running on the host, and tells the
chat when it trades.

The trading-repo half is written up as a task prompt for a session in that
repo: `../prompt-trading-live.md` (outside both repos).

---

## What this deviates from

- **"The assistant does not execute trades"** (spec, overview).
- **§10** — no `mt5.open_trade()` / `close_trade()` / `modify_trade()` in V1.
- `mcp_server/server.py` docstring and `README.md`: *"There is no tool that can
  place, modify or close a trade, and none will be added."*

The model will not get order tools. It gets tools that start and stop a
process which trades by itself, by the strategy's rules. That still means the
assistant can put money at risk, so the guarantee text must change wherever it
is stated rather than stay quietly untrue. Like `backtests.run`
([BACKTEST_EXECUTION.md](BACKTEST_EXECUTION.md)), this is a POC deviation.

### Temporary: one shared test account, open to every user

The runner trades the **shared account** in `trading/mt5-mcp-server/config.json`
— the founder's MetaQuotes demo — not the account a user has linked. Because
nothing is at stake but a demo balance, **any paired user may start or stop
it**, and it starts **live, without a confirmation step**.

That only holds while all three stay true. Before any of them changes:

- **A real or customer account** — start/stop must be restricted to operators,
  checked by the server from the caller's identity, and going live must need
  an explicit confirmation.
- **A user's own account** — the runner must trade through that user's
  credentials, on a terminal of its own (see *The shared terminal*).
- **More users than the founder's circle** — who receives which notification
  must be a per-user setting.

The real-money guard is the one piece that makes "test account" enforceable:
the runner refuses a real-money account unless given `--allow-real`, and the
agents side never passes it.

---

## Decisions

| | Question | Decision |
|---|---|---|
| 1 | How is the runner started and stopped? | **MCP tools the model can call.** |
| 2 | Whose account does it trade? | **The shared test account** in `trading/mt5-mcp-server/config.json`. Never a user's linked account. Temporary. |
| 3 | Who may start and stop it? | **Any paired user.** Temporary. |
| 4 | Paper or live by default? | **Live** (`paper=false`), no confirmation. Paper is available on request. |
| 5 | Real money? | **Refused.** `live.start` never passes `--allow-real`. |
| 6 | Who is notified? | **Every paired user, for now.** Later: configurable per user — which runner, which event kinds. |
| 7 | What is sent? | **Order events as they happen, plus a daily status.** A status is also available on request, and can be built from mock data to test the message. |
| 8 | When is the daily status sent? | **Every day, weekends included.** |
| 9 | Stale margin data? | **Warn in the status** (daily and on request). Never blocks startup. |

Working defaults, proposed 2026-09-15 and not objected to:

| | Default |
|---|---|
| Stale margin threshold | Newest reading for the strategy's contract older than **30 days**. |
| Daily status time | **21:30 UTC** — half an hour from an H4 bar boundary in summer and winter alike. It was 21:00 until 2026-09-18; see *First days live*. |
| Shadow and paper events | **Sent**, every message marked as simulated. |
| The shared terminal | **Account guard now**; a dedicated terminal before a second user links an account. |

---

## How the runner behaves (trading repo, as built)

- `run-live.sh` runs `run_live.py` under the Wine Python, attached to the
  running terminal, logged in with the shared account. It refuses a real-money
  account without `--allow-real`.
- Startup replays every bar since the config's `start` through the backtest
  engine, then steps each new H4 bar. It stays in **shadow** (sends nothing)
  until the account holds what the replay holds; `--paper` stays in shadow.
- Stops and targets live on the broker's server. Stopping leaves positions open
  and removes resting limit orders.
- The session directory `runs-live/<strategy>_<symbol>_<magic>/` holds
  `events.jsonl` (every event, also printed to the console) and `state.json`
  (the snapshot, rewritten on every poll). Kinds: `started`, `mode`, `stopped`,
  `error`, `bar_closed`, `order_intent`, `order_placed`, `order_filled`,
  `order_rejected`, `order_cancelled`, `position_modified`, `exit_intent`,
  `position_closed`. Each carries `mode`. A retrying `error` is written once,
  not again while the same message repeats within five minutes.
- **The contract** — every event's fields, every `state.json` field, stop and
  exit status — is the trading README's section *The session, as reporting
  reads it*. That section, not this plan, is the reference.

## The shared terminal

The host runs one MT5 terminal. Three things log it in:

- the bridge (`mt5_bridge/bridge.py`), into whichever account a user's request
  names, serialised by a lock that exists only inside the bridge process;
- `fetch-mt5.sh`, used by `backtests.run` and `backtests.fetch_data`, into the
  shared account, under `/tmp/mt5.lock`;
- the live runner, into the shared account, permanently, under no lock.

Today the bridge's only account is the shared one, so nothing switches. Once a
second user links an account, the bridge would switch the terminal under the
runner. The account guard (Phase 1) stops orders reaching the wrong account,
but trading stalls on every switch — hence a dedicated terminal before that
happens. Also before then: the guard's expected login is whatever the terminal
is on when the replay starts, not the login in `config.json`; a switch in the
seconds between logging in and starting would make the runner guard the wrong
account. Pin it to the configured login. See [MT5.md](MT5.md) for the
untested multi-account question.

---

## Phase 0 — host prerequisites

1. **Done.** Trading `4879052` and agents pulled on the server.
2. **Done.** PyYAML 6.0.3 installed into the server's Wine Python.
3. **Done.** Algo Trading enabled in the server terminal, 2026-09-15: an
   `[Experts]` section with the workstation's values (`Enabled=1`,
   `Account=1`, …) added to `Config/common.ini` with the terminal stopped; the
   original is kept beside it as `common.ini.bak-20260915-before-algo`.
   `terminal_info().trade_allowed` is true on the shared demo login.
   `Account=1` switches automated trading off if the terminal changes account —
   a guard worth keeping on a terminal the bridge can re-log.
4. **Not done.** `data/margins/margins.csv` on the server ends 2026-05-01. CME
   refuses scripted downloads, so this is the PDF from a browser and
   `scripts/margins.py import`, by hand. Until then the status warns.

## Phase 1 — the runner (trading repo) — built

Specified in `../prompt-trading-live.md`; implemented as specified, 408 tests
passing. A paper run on the server terminal (2026-09-15) logged in, replayed
7,320 bars (120 trades, ending with one position open), wrote `state.json` with
the margin warning, and stopped cleanly on SIGTERM. Live sends are not yet
exercised on a real terminal. In short:

1. **Account guard.** Check the login before every `order_send`. A mismatch
   sends nothing, emits `error`, and is handled as the terminal being
   unavailable — never as `order_rejected`.
2. **Sends lost to a dropped connection.** `None` from `order_send` means
   unknown: re-read the account, adopt the order if it went through, otherwise
   retry within the bar and reject only at the bar boundary. `sync` reports a
   position or order carrying the magic number that the runner does not track.
3. **`runs-live/<session>/state.json`**, written atomically on every poll:
   heartbeat, running, mode, paper, config, account, last bar, balance, equity,
   positions, resting and queued orders (marked simulated in shadow), the
   margin block (`as_of`, `age_days`, `stale`), last error. The margin block
   also goes into `started`.
4. **Startup failures are visible**: an `error` event with `retrying: false`
   and a `state.json` with the reason, then a non-zero exit.
5. **`run-live.sh`** kills only its own runner, and stops cleanly on a SIGTERM
   to the wrapper alone.
6. Tests against `tests/fake_mt5.py`; the README records the contract below.

**The contract agents relies on:** sessions are found by scanning
`runs-live/*/state.json` and matching `config`; stop is SIGTERM to
`run-live.sh`, clean within 60 s; exit 0 after a requested stop, non-zero on
failure; event kinds and `state.json` fields as the trading README documents.

## Phase 2 — process control (agents) — built

1. **Unit** `deploy/units/live-trader@.service`, one instance per config
   (`live-trader@mz50` → `configs/strategies/mz50.yaml`). `LIVE_CONFIG` and
   `LIVE_ARGS` (`--paper` or empty) come from
   `~/.config/trading-assistant/live/<instance>.env`, which `live.start` writes,
   so the tool chooses paper or live without editing the unit.
   - `KillMode=mixed`, `TimeoutStopSec=90`: the wrapper stops cleanly on
     SIGTERM within 60 s; the default kill reaches the Wine Python at once and
     skips withdrawing limit orders.
   - `Restart=on-failure` after 60 s, at most three starts in 30 minutes: a
     runner that cannot start fails the same way each time, and every failure
     is a message to every chat.
   - `live.start` enables the instance and `live.stop` disables it, so a
     running trader comes back after a reboot and a stopped one does not.
   - **Not** part of `trading-assistant.target`: restarting the bot must never
     stop trading.
   - Installed by `deploy/install.sh --headless` only. A workstation runner on
     the same shared account would close the server's positions as leftovers.
2. **MCP tools** in `mcp_server/server.py`, reading through
   `mcp_server/live_sessions.py`, callable by any user:
   - `live.status(config?)` — read-only. Per runner: `condition` (running /
     starting / stuck / restarting / failed / stopped / never started), `mode`
     (live / shadow / paper), heartbeat age, balance, equity, positions and
     orders (flagged `simulated` in shadow), the last 24 hours' counts, the
     margin warning, and `text`, the same status message the daily status
     sends. Lists `startable_configs`.
   - `live.start(config, paper=false)` — refuses an unknown config, a host
     without the unit, a runner already running, and a second config trading
     the same strategy and symbol (they would share a magic number). Never
     passes `--allow-real`. Waits up to 3 minutes for this start's state file
     and returns its status, or the failure, or "still starting".
   - `live.stop(config)` — blocks through the clean stop; returns
     `positions_left_open`.
   - Start and stop annotated `read_only_hint=False`, `destructive_hint=True`.
3. **Briefing and docs.** `agent-workspace/TOOLS.md` gains *The live trader*:
   shared account not the user's, start or stop only when asked, shadow is not
   trading, simulated positions are not trades, stopping closes nothing, pass
   on the margin warning. `SOUL.md`, the `server.py` docstring and `README.md`
   now state the guarantee as it is: no tool places, closes or modifies a trade.
   `deploy/README.md` and `docs/DEPLOYMENT.md` cover the units.
4. **Tests.** `tests/smoke-mcp.sh` expects the three tools and calls them
   (status with nothing running, start and stop with an unknown config).
   Eval probes L1–L4 in `tests/agent-evals/behaviour.md`, not yet run; only L1
   is in `run-evals.sh`, since L2–L4 start or stop the runner on the server.

## Phase 3 — notifications (agents) — built

1. **`apps/live_notify/`, its own unit** (`live-notify.service`, in the
   target). Reads each session's `events.jsonl` from a saved offset and sends
   to Telegram, advancing the offset only after delivery, so a restart or an
   outage delays messages instead of losing them. A failure Telegram calls
   permanent (400/403: chat gone, bot blocked) is not retried; anything else
   is, with backoff. The offsets live in
   `~/.local/state/trading-assistant/live-notify.json` (`LIVE_NOTIFY_STATE`);
   on its very first run the notifier skips what is already written.
2. **Sent immediately:** `order_filled`, `order_placed`, `order_cancelled`,
   `order_rejected`, `position_modified`, `position_closed`, and lifecycle
   `started`, `mode`, `stopped`, `error`. The same error text from one session
   is sent once an hour at most. **Not sent:** `order_intent`, `exit_intent`,
   `bar_closed`. Shadow and paper order events are sent, marked simulated.
3. **Daily status** every day at 21:30 UTC (`LIVE_STATUS_UTC`), one message per
   runner that is running or stopped within the last 24 hours. Stuck, failed or
   gone-without-a-report comes first; a stale margin reading is a warning line.
   It waits, up to 5 minutes, while a runner's `state.json` is older than its
   latest event, so it never mixes a snapshot from before a step with events
   from after it. "Last 24h" counts only what happened on the account; the
   backtest's fills and closes, while in shadow or paper, get their own
   "Simulated 24h" line.
4. **Recipients** are one function, `recipients()` in
   `apps/live_notify/main.py`: every paired user who is not disabled. A
   subscription table (user × runner × event kinds) replaces its body later;
   it already receives the event or status it is resolving for.
5. **Status on request and mock status.** One formatter
   (`live_sessions.status_text`) serves the daily status, `live.status` and:
   ```bash
   scripts/live-notify.sh status --mock                    # print, from tests/fixtures/live/
   scripts/live-notify.sh events --mock                    # every event message
   scripts/live-notify.sh status --mock --send-to <chat>   # the mock, to one chat
   scripts/live-notify.sh status --send-to <chat>          # the real status, now
   scripts/live-notify.sh status --send-all                # to everyone
   ```
   Every `--mock` message starts `MOCK — sample data, not a real trader`.
   `tests/test-live.sh` (free: no model, database or Telegram) holds the
   summaries, messages, file reading and delivery rules to the fixtures.

As built, from `status --mock` and `events --mock` (without their `MOCK` line):

```text
mz50 · EURUSD H4 — daily status, 16 Sep 21:30 UTC
Runner   running 28h 42m · LIVE · heartbeat 12s ago · last bar 16 Sep 16:00 broker time
Account  balance 100,412.50 · equity 100,380.10 (demo, MetaQuotes-Demo)
Open     SELL 0.1 @ 1.1742 · SL 1.1811 · TP 1.168 · -32.40
Resting  none
Last 24h 1 filled · 1 closed (+62.40) · 2 rejected · 2 errors
Simulated 24h 0 filled · 1 closed (+41.30) — the backtest's, not on the account
Last error  positions_get: IPC timeout (-10005)
⚠ Margin data is 138 days old (6E, 2026-05-01) — zones may use an outdated margin.
```

```text
mz50 · EURUSD — now trading live
sending BUY 0.1 at market
```

```text
mz50 · EURUSD · live — order rejected
BUY 0.1 limit 1.1695 — not sent before its bar ended: Market closed (retcode 10018)
```

## First days live

Started live on the server 2026-09-15 21:02 UTC.

- **The first live order was lost.** On 16 Sep the replay's position closed
  and the runner went live at the next open — 00:00 on the broker's clock, the
  daily break. Its SELL limit was answered "Market closed" (10018), which the
  runner then treated as final: rejected, never resent. mz50 decides at the
  daily rollover, so its orders go out at exactly that moment every time. The
  trading repo now keeps a "not now" answer and resends within the bar
  (`../prompt-trading-rollover.md`). Until the runner is restarted on that
  code, the account lacks the limit the backtest still holds; a restart
  replays and places it at the next open.
- **The 16 Sep daily status contradicted the messages above it.** It was
  composed at 21:00 UTC, the same second the H4 bar closed, while the runner
  sat 19 s in the refused send: `state.json` still showed the step before
  (SHADOW, the SELL open) while the events already said closed and live.
  Hence 21:30 and the wait for a current snapshot.
- **A simulated close read as a trade.** Both daily statuses counted the
  backtest's +87.20 in "Last 24h", the second in LIVE mode with the balance
  untouched at 100,000.00. Hence the separate "Simulated 24h" line.
- **A mock status was taken for a real one.** Hence the `MOCK` line.

## Phase 4 — roll out

1. **Done 2026-09-15.** Both repos pulled, `deploy/install.sh --headless`,
   `trading-assistant.target` restarted with `live-notify`; smoke test and
   `tests/test-live.sh` green on the server.
2. `scripts/live-notify.sh status --mock --send-to <founder chat>`; then
   `--send-all`.
3. `live.start(mz50, paper=true)` on the host through the bot, as a first
   check. Leave it across at least one bar close and one daily status.
4. Stop, then `live.start(mz50)` — live on the shared demo account, once
   Algo Trading is on. The replay currently ends holding a position, so the
   runner starts in shadow and trades live only after the backtest closes it.
   Watch the first fill end to end: event → Telegram → `live.status`.

---
