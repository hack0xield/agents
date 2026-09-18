"""The live traders' Telegram notifications, and previews of what they send.

    scripts/live-notify.sh run                              the service
    scripts/live-notify.sh status [--mock] [--config mz50]  print a status now
    scripts/live-notify.sh events [--mock] [--last 20]      print how events read

`status` and `events` send nothing unless given `--send-to CHAT_ID` (one chat;
for a private chat that is the user's Telegram id) or `--send-all` (everyone
the service would tell). `--mock` reads tests/fixtures/live/ instead of the
real sessions, so the messages can be checked with no runner at all.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(1, str(REPO / "mcp_server"))
sys.path.insert(2, str(REPO / "apps" / "orchestrator"))

import live_sessions  # noqa: E402
from notifier import STATUS_AT, STATUS_WINDOW, Notifier, Progress  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "live"
#: Leads every message built from the fixtures, so sample data is never
#: mistaken for a real trader.
MOCK = "MOCK — sample data, not a real trader"
PROGRESS = Path(os.environ.get("LIVE_NOTIFY_STATE")
                or Path.home() / ".local" / "state" / "trading-assistant" / "live-notify.json"
                ).expanduser()

log = logging.getLogger("live-notify")


def recipients(subject: dict) -> list[int]:
    """Every paired user who is not disabled.

    For now everyone hears everything. Per-user settings — which runner, which
    kinds — replace this function and nothing else.
    """
    import db
    import models
    from sqlalchemy import select

    with db.session_scope() as s:
        chats = s.scalars(
            select(models.TelegramIdentity.chat_id)
            .join(models.User, models.User.id == models.TelegramIdentity.user_id)
            .where(models.User.disabled.is_(False))
        ).all()
    return sorted({int(c) for c in chats})


def send(chat_id: int, text: str) -> str:
    import telegram

    return telegram.deliver(chat_id, text)


def status_time() -> clock_time:
    configured = os.environ.get("LIVE_STATUS_UTC")
    if not configured:
        return STATUS_AT
    hours, _, minutes = configured.partition(":")
    return clock_time(int(hours), int(minutes or 0))


def token_or_exit() -> None:
    import config

    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")


# ------------------------------------------------------------------ commands

def run(args) -> int:
    token_or_exit()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    notifier = Notifier(send, recipients, Progress(PROGRESS), status_at=status_time(),
                        sleep=stop.wait, stopping=stop.is_set)
    log.info("watching %s; daily status at %s UTC; progress in %s",
             live_sessions.LIVE_DIR, notifier.status_at.strftime("%H:%M"), PROGRESS)
    notifier.run()
    log.info("stopped")
    return 0


def status(args) -> int:
    if args.mock:
        state = live_sessions.read_json(FIXTURES / "state.json")
        now = live_sessions.parse_time(state["updated_at"]) + timedelta(seconds=12)
        events = [e for e in live_sessions.tail(FIXTURES / "events.jsonl", 0)[0]
                  if live_sessions.parse_time(e["time"]) >= now - STATUS_WINDOW]
        summary = live_sessions.summarize(state, events, None, now, state["config"])
        texts = [f"{MOCK}\n{live_sessions.status_text(summary, now, 'daily status')}"]
    else:
        config = f"configs/strategies/{Path(args.config).stem}.yaml" if args.config else None
        notifier = Notifier(send, recipients, Progress(PROGRESS))
        texts = notifier.statuses(datetime.now(timezone.utc), recent_only=False, config=config)
        if not texts:
            print("no live trader has written a state file yet")
    return show_and_send(texts, args)


def events(args) -> int:
    if args.mock:
        found = live_sessions.tail(FIXTURES / "events.jsonl", 0)[0]
    else:
        found = []
        for directory in live_sessions.sessions():
            state = live_sessions.read_json(directory / "state.json") or {}
            if args.config and Path(state.get("config") or "").stem != Path(args.config).stem:
                continue
            found += live_sessions.tail(directory / "events.jsonl", 0)[0]
        found.sort(key=lambda e: e.get("time", ""))
    found = found[-args.last:]
    texts = []
    for event in found:
        text = live_sessions.event_text(event)
        if text is None:
            print(f"-- {event.get('time')} {event.get('kind')}: not sent\n")
        else:
            text = f"{MOCK}\n{text}" if args.mock else text
            texts.append(text)
            print(f"-- {event.get('time')} {event.get('kind')}")
            print(text + "\n")
    return show_and_send(texts, args, printed=True)


def show_and_send(texts: list[str], args, printed: bool = False) -> int:
    if not printed:
        for text in texts:
            print(text + "\n")
    if not (args.send_to or args.send_all) or not texts:
        return 0
    token_or_exit()
    chats = [int(args.send_to)] if args.send_to else recipients({"kind": "preview"})
    failures = 0
    for chat in chats:
        outcomes = [send(chat, text) for text in texts]
        bad = [o for o in outcomes if o != "sent"]
        failures += len(bad)
        print(f"{chat}: {len(outcomes) - len(bad)} of {len(outcomes)} sent"
              + (f" ({', '.join(sorted(set(bad)))})" if bad else ""))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="the notification service")
    for name, helptext in (("status", "print a status message now"),
                           ("events", "print how recent events read as messages")):
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("--mock", action="store_true", help="read tests/fixtures/live/")
        sub.add_argument("--config", help="one run config, e.g. mz50")
        target = sub.add_mutually_exclusive_group()
        target.add_argument("--send-to", metavar="CHAT_ID", help="send to this chat")
        target.add_argument("--send-all", action="store_true",
                            help="send to everyone the service tells")
        if name == "events":
            sub.add_argument("--last", type=int, default=20, help="how many events (default 20)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return {"run": run, "status": status, "events": events}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
