#!/usr/bin/env bash
# The live trader's reporting: status summaries, event messages, reading the
# events file, and the notifier's delivery rules — against the fixture session
# in tests/fixtures/live/, never a real runner.
#
# The tools are called only with config names that do not exist, so nothing is
# ever started or stopped, on a workstation or on the server.
#
# No model, no API key, no database, no Telegram.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

exec "$REPO/.venv/bin/python" - <<'PY'
import json, shutil, sys, tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "mcp_server")
sys.path.insert(1, "apps/live_notify")
import live_sessions as ls
import notifier as nt

FIX = Path("tests/fixtures/live")
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {str(detail)[:300]}" if detail and not ok else ""))
    if not ok:
        fails += 1


state = json.loads((FIX / "state.json").read_text())
events = ls.tail(FIX / "events.jsonl", 0)[0]
at = ls.parse_time(state["updated_at"])
day = [e for e in events if ls.parse_time(e["time"]) >= at - timedelta(hours=24)]

# ── the summary ─────────────────────────────────────────────────────────────
s = ls.summarize(state, day, None, at + timedelta(seconds=12))
check("a fresh heartbeat is running", s["condition"] == "running", s["condition"])
check("mode is live", s["mode"] == "live")
check("last 24h counts", s["last_24h"] == {"filled": 1, "closed": 1, "net_pnl": 62.4,
                                            "rejected": 2, "cancelled": 1, "errors": 2},
      s["last_24h"])
check("a leftover close is not counted as a trade", s["last_24h"]["closed"] == 1)
check("the backtest's close is kept apart from the account's",
      s["simulated_24h"]["closed"] == 1 and s["simulated_24h"]["net_pnl"] == 41.3,
      s["simulated_24h"])
check("and reads as the backtest's", "Simulated 24h 0 filled · 1 closed (+41.30) — the backtest's"
      in s["text"], s["text"])
shadow_error = {"time": state["updated_at"], "kind": "error", "mode": "shadow", "error": "x",
                "retrying": True}
check("an error in shadow is still the runner's own",
      ls.summarize(state, day + [shadow_error], None, at)["last_24h"]["errors"] == 3)
quiet = [e for e in day if e.get("mode") != "shadow"]
check("no simulated line without simulated trades",
      "Simulated 24h" not in ls.summarize(state, quiet, None, at)["text"])
check("stale margin is warned", s["margin_warning"] and "138 days" in s["margin_warning"])
check("the text carries the open position", "SELL 0.1 @ 1.1742" in s["text"], s["text"])

s = ls.summarize(state, day, None, at + timedelta(minutes=10))
check("no heartbeat for 10 minutes is stuck", s["condition"] == "stuck", s["condition"])
check("stuck is said first", s["text"].splitlines()[1].startswith("⚠ No heartbeat"), s["text"])

s = ls.summarize(state, day, {"active_state": "inactive"}, at + timedelta(seconds=12))
check("running on file but the unit is gone", s["condition"] == "ended without a final report")

s = ls.summarize(state, day, {"active_state": "active"}, at + timedelta(seconds=12))
check("an active unit with a fresh heartbeat is running", s["condition"] == "running")

stopped = {**state, "running": False, "stopped_at": state["updated_at"], "failure": "boom"}
check("a failure is failed", ls.summarize(stopped, [], None, at)["condition"] == "failed")
check("a failure while systemd restarts it is restarting",
      ls.summarize(stopped, [], {"active_state": "activating"}, at)["condition"] == "restarting")
check("a requested stop is stopped",
      ls.summarize({**stopped, "failure": None}, [], None, at)["condition"] == "stopped")
check("replaying is starting", ls.summarize({**state, "mode": None}, [], None, at)["condition"] == "starting")
check("no state is never started", ls.summarize(None, [], None, at)["condition"] == "never started")

replay = {**state, "mode": "shadow", "source": "replay"}
s = ls.summarize(replay, [], None, at + timedelta(seconds=5))
check("the replay's book is simulated", s["simulated"] and "Simulated" in s["text"], s["text"])
paper = {**state, "mode": "shadow", "paper": True, "source": "replay"}
check("paper is reported as paper", ls.summarize(paper, [], None, at)["mode"] == "paper")

# ── event messages ──────────────────────────────────────────────────────────
kinds = {e["kind"] for e in events}
for e in events:
    text = ls.event_text(e)
    if e["kind"] in ls.NOTIFY_KINDS:
        check(f"{e['kind']} has a message", bool(text))
    else:
        check(f"{e['kind']} is not sent", text is None)
check("the fixture covers every kind that is sent", ls.NOTIFY_KINDS - {"stopped"} <= kinds,
      ls.NOTIFY_KINDS - kinds)
shadow_fill = next(e for e in events if e["kind"] == "order_filled" and e["mode"] == "shadow")
check("a shadow fill says simulated", "simulated" in ls.event_text(shadow_fill))
live_fill = next(e for e in events if e["kind"] == "order_filled" and e["mode"] == "live")
check("a live fill says live", "· live —" in ls.event_text(live_fill))
rejected = next(e for e in events if e["kind"] == "order_rejected" and not e.get("action"))
check("a rejected limit says it was a limit, and where", "BUY 0.1 limit 1.1695 —"
      in ls.event_text(rejected), ls.event_text(rejected))
mode = next(e for e in events if e["kind"] == "mode")
check("going live says what it is sending", "sending BUY 0.1 at market" in ls.event_text(mode),
      ls.event_text(mode))
cancelled = next(e for e in events if e["kind"] == "order_cancelled")
check("a cancelled order names it and says why in plain words",
      "BUY 0.1 limit 1.1695 — the price reached the level that voids it"
      in ls.event_text(cancelled), ls.event_text(cancelled))
check("a reason with no plain wording is passed through",
      "— broker said no" in ls.event_text({**cancelled, "reason": "broker said no"}))
check("a stop after a failure says why",
      "boom" in ls.event_text({"kind": "stopped", "mode": "live", "failure": "boom"}))

# ── reading the events file ─────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
f = tmp / "events.jsonl"
f.write_text('{"kind": "a"}\nnot json\n{"kind": "b"}\n{"kind": "c"')
found = ls.entries(f, 0)
check("a half-written line is left for later", [e and e["kind"] for e, _ in found] == ["a", None, "b"],
      found)
check("offsets stop after the last complete line", found[-1][1] == len('{"kind": "a"}\nnot json\n{"kind": "b"}\n'))
f.write_text('{"kind": "z"}\n')
check("a file shorter than the offset is read from the start",
      [e["kind"] for e, _ in ls.entries(f, 1000)] == ["z"])

# ── sessions and env files ──────────────────────────────────────────────────
live = tmp / "runs-live"
for name, updated in (("mz50_EURUSD_1", "2026-09-16T21:00:00+00:00"),
                      ("mz50_EURUSD_2", "2026-09-10T21:00:00+00:00")):
    (live / name).mkdir(parents=True)
    (live / name / "state.json").write_text(json.dumps({**state, "updated_at": updated}))
check("the newest session for a config wins",
      ls.session_for("configs/strategies/mz50.yaml", live).name == "mz50_EURUSD_1")
env = ls.write_env("mz50", Path("configs/strategies/mz50.yaml"), paper=False, env_dir=tmp / "env")
check("a live start passes no arguments", "LIVE_ARGS=\n" in env.read_text(), env.read_text())
env = ls.write_env("mz50", Path("configs/strategies/mz50.yaml"), paper=True, env_dir=tmp / "env")
check("a paper start passes --paper", "LIVE_ARGS=--paper\n" in env.read_text())
check("--allow-real is never written", "allow-real" not in env.read_text())
check("a config stem is an instance", ls.instance_for(Path("mz50.yaml")) == "mz50")
check("a snapshot older than the last event is behind",
      ls.snapshot_behind({"updated_at": "2026-09-16T19:00:00+00:00"}, FIX / "events.jsonl"))
check("a snapshot newer than the last event is not",
      not ls.snapshot_behind(state, FIX / "events.jsonl"))
check("a name systemd would mangle is refused", ls.instance_for(Path("a b.yaml")) is None)

# ── the notifier ────────────────────────────────────────────────────────────
def notifier_in(directory, outcomes=None, now=None, stop_after=None):
    sent, sleeps = [], []
    queue = list(outcomes or [])

    def send(chat, text):
        outcome = queue.pop(0) if queue else nt.SENT
        sent.append((chat, text, outcome))
        return outcome

    def sleep(seconds):
        sleeps.append(seconds)

    n = nt.Notifier(send, lambda subject: [111, 222], nt.Progress(directory / "progress.json"),
                    live_dir=directory / "runs-live", clock=lambda: now or at,
                    sleep=sleep, ask_systemd=False,
                    stopping=(lambda: len(sleeps) >= stop_after) if stop_after else (lambda: False))
    return n, sent, sleeps


work = Path(tempfile.mkdtemp())
session = work / "runs-live" / "mz50_EURUSD_954607734"
session.mkdir(parents=True)
shutil.copy(FIX / "events.jsonl", session / "events.jsonl")
shutil.copy(FIX / "state.json", session / "state.json")

n, sent, _ = notifier_in(work)
n.begin()
n.tell_events()
check("the first run does not replay history", sent == [], len(sent))

with open(session / "events.jsonl", "a") as fh:
    for e in ({"time": "2026-09-16T22:00:00+00:00", "kind": "bar_closed", "mode": "live"},
              {**live_fill, "time": "2026-09-16T22:00:01+00:00"},
              {"time": "2026-09-16T22:01:00+00:00", "kind": "error", "mode": "live",
               "strategy": "mz50", "symbol": "EURUSD", "error": "IPC timeout", "retrying": True},
              {"time": "2026-09-16T22:02:00+00:00", "kind": "error", "mode": "live",
               "strategy": "mz50", "symbol": "EURUSD", "error": "IPC timeout", "retrying": True}):
        fh.write(json.dumps(e) + "\n")
n, sent, _ = notifier_in(work)
n.tell_events()
texts = [t for _, t, _ in sent]
check("a new fill reaches every recipient", sum("order filled" in t for t in texts) == 2, texts)
check("bar_closed is not sent", not any("bar" in t for t in texts))
check("the same error inside an hour is told once", sum("IPC timeout" in t for t in texts) == 2, texts)
progress = json.loads((work / "progress.json").read_text())
check("the offset reaches the end of the file",
      progress["offsets"]["mz50_EURUSD_954607734"] == (session / "events.jsonl").stat().st_size)

with open(session / "events.jsonl", "a") as fh:
    fh.write(json.dumps({**live_fill, "time": "2026-09-16T22:30:00+00:00"}) + "\n")
n, sent, sleeps = notifier_in(work, outcomes=[nt.RETRY, nt.SENT, nt.SENT])
n.tell_events()
check("a passing failure is retried until delivered",
      [(c, o) for c, _, o in sent] == [(111, "retry"), (222, "sent"), (111, "sent")], sent)
check("only the failed chat is retried", len(sent) == 3 and sleeps, sleeps)

with open(session / "events.jsonl", "a") as fh:
    fh.write(json.dumps({**live_fill, "time": "2026-09-16T22:40:00+00:00"}) + "\n")
before = json.loads((work / "progress.json").read_text())["offsets"]["mz50_EURUSD_954607734"]
n, sent, _ = notifier_in(work, outcomes=[nt.RETRY] * 10, stop_after=1)
n.tell_events()
after = json.loads((work / "progress.json").read_text())["offsets"]["mz50_EURUSD_954607734"]
check("an undelivered event keeps its place when stopping", before == after, (before, after))

n, sent, _ = notifier_in(work, outcomes=[nt.REFUSED, nt.SENT])
n.tell_events()
check("a refused chat is not retried", [(c, o) for c, _, o in sent] == [(111, "refused"), (222, "sent")],
      sent)

def at_utc(day, hour, minute, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=timezone.utc)

n, sent, _ = notifier_in(work, now=at_utc(16, 21, 29))
n.tell_daily_status()
check("no daily status before 21:30", sent == [])
# The events appended above are newer than the session's state.json.
n, sent, _ = notifier_in(work, now=at_utc(16, 21, 30, 12))
n.tell_daily_status()
check("the daily status waits while a snapshot is behind its events", sent == [])
n, sent, _ = notifier_in(work, now=at_utc(16, 21, 35, 30))
n.tell_daily_status()
check("and goes to everyone once the wait is over",
      len(sent) == 2 and "daily status" in sent[0][1], [t for _, t, _ in sent])
n.tell_daily_status()
check("only once that day", len(sent) == 2)

fresh = {**state, "updated_at": "2026-09-17T21:30:00+00:00"}
(session / "state.json").write_text(json.dumps(fresh))
n, sent, _ = notifier_in(work, now=at_utc(17, 21, 30, 5))
n.tell_daily_status()
check("a current snapshot is sent at once", len(sent) == 2, [t for _, t, _ in sent])

old = {**state, "running": False, "stopped_at": "2026-09-10T10:00:00+00:00"}
(session / "state.json").write_text(json.dumps(old))
n, sent, _ = notifier_in(work, now=at_utc(18, 21, 36))
n.tell_daily_status()
check("a runner stopped days ago gets no daily status", sent == [], [t for _, t, _ in sent])

# ── previews ────────────────────────────────────────────────────────────────
import subprocess
for command in (["status", "--mock"], ["events", "--mock", "--last", "3"]):
    out = subprocess.run([sys.executable, "apps/live_notify/main.py", *command],
                         capture_output=True, text=True).stdout
    messages = [m for m in out.split("\n\n") if m.strip() and "not sent" not in m]
    check(f"every {command[0]} --mock message says it is sample data",
          messages and all("MOCK — sample data" in m for m in messages), out)

# ── the tools, without starting anything ────────────────────────────────────
import server
server.CALL_LOG = tmp / "tool-calls.jsonl"
r = server.live_start("no-such-config")
check("start refuses an unknown config", r["ok"] is False and "mz50.yaml" in r["configs"], r)
r = server.live_stop("no-such-config")
check("stop refuses an unknown config", r["ok"] is False, r)
r = server.live_status("no-such-config")
check("status refuses an unknown config", r.get("ok") is False, r)
r = server.live_status()
check("status answers with nothing running", "runners" in r and "account_note" in r, r)

print("\nFAILED" if fails else "\nAll green")
sys.exit(1 if fails else 0)
PY
