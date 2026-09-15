"""Tell people what the live traders do: order events as they happen, and a
daily status.

Each session's events.jsonl is read from a saved offset, and the offset moves
only once an event's message is delivered, so a restart or a Telegram outage
delays messages instead of losing them. Delivery is at least once: a message
cut off by a stop is sent again.

Who hears what is `recipients`, one function, so per-user settings can replace
"every paired user" without touching anything else here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Callable

import live_sessions

log = logging.getLogger("live-notify")

SENT, RETRY, REFUSED = "sent", "retry", "refused"
#: The same error text from one session is told once an hour at most.
ERROR_REPEAT_SECONDS = 3600
#: A runner that stopped longer ago than this gets no daily status.
STATUS_WINDOW = timedelta(hours=24)

Send = Callable[[int, str], str]
Recipients = Callable[[dict], list[int]]


class Progress:
    """Where reading stopped in each events.jsonl, and the last daily status."""

    def __init__(self, path: Path):
        self.path = path
        data = live_sessions.read_json(path)
        self.fresh = data is None
        data = data or {}
        self.offsets: dict[str, int] = {k: int(v) for k, v in (data.get("offsets") or {}).items()}
        self.status_sent_on: str | None = data.get("status_sent_on")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps({"offsets": self.offsets,
                                         "status_sent_on": self.status_sent_on}, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.path)


class Notifier:
    def __init__(
        self,
        send: Send,
        recipients: Recipients,
        progress: Progress,
        live_dir: Path | None = None,
        status_at: clock_time = clock_time(21, 0),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        stopping: Callable[[], bool] = lambda: False,
        ask_systemd: bool = True,
    ):
        self.send = send
        self.recipients = recipients
        self.progress = progress
        self.live_dir = live_dir
        self.status_at = status_at
        self.clock = clock
        self.sleep = sleep
        self.stopping = stopping
        self.ask_systemd = ask_systemd
        self._errors_told: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ loop

    def run(self, poll_seconds: float = 5.0) -> None:
        self.begin()
        while not self.stopping():
            try:
                self.tell_events()
                self.tell_daily_status()
            except Exception:
                log.exception("notification pass failed")
            self.sleep(poll_seconds)

    def begin(self) -> None:
        """On the very first run, start at the end of every existing session.

        What happened before the notifier existed is history, not news. A
        session that appears later is read from its first event.
        """
        if not self.progress.fresh:
            return
        for directory in live_sessions.sessions(self.live_dir):
            path = directory / "events.jsonl"
            self.progress.offsets[directory.name] = path.stat().st_size if path.exists() else 0
        self.progress.fresh = False
        self.progress.save()

    # ---------------------------------------------------------------- events

    def tell_events(self) -> None:
        for directory in live_sessions.sessions(self.live_dir):
            name = directory.name
            offset = self.progress.offsets.get(name, 0)
            for event, after in live_sessions.entries(directory / "events.jsonl", offset):
                text = live_sessions.event_text(event) if event else None
                if text and not self._repeated_error(name, event):
                    if not self.deliver(text, {**event, "session": name}):
                        return                           # stopping; this event is still owed
                self.progress.offsets[name] = after
                self.progress.save()

    def _repeated_error(self, session: str, event: dict) -> bool:
        if event.get("kind") != "error":
            return False
        key = (session, str(event.get("error")))
        now = time.monotonic()
        told = self._errors_told.get(key)
        if told is not None and now - told < ERROR_REPEAT_SECONDS:
            return True
        self._errors_told[key] = now
        return False

    # ---------------------------------------------------------------- status

    def tell_daily_status(self) -> None:
        now = self.clock()
        today = now.date().isoformat()
        if now.time() < self.status_at or self.progress.status_sent_on == today:
            return
        for text in self.statuses(now, label="daily status"):
            if not self.deliver(text, {"kind": "daily_status"}):
                return
        self.progress.status_sent_on = today
        self.progress.save()

    def statuses(self, now: datetime, label: str = "status", recent_only: bool = True,
                 config: str | None = None) -> list[str]:
        """A status message per session: every one, or those running or lately stopped."""
        texts = []
        for directory in live_sessions.sessions(self.live_dir):
            state = live_sessions.read_json(directory / "state.json")
            if not state or (config and state.get("config") != config):
                continue
            instance = live_sessions.instance_for(Path(state.get("config") or ""))
            unit = live_sessions.unit_state(instance) if (self.ask_systemd and instance) else None
            stopped = live_sessions.parse_time(state.get("stopped_at"))
            recent = (state.get("running") or live_sessions.unit_active(unit)
                      or (stopped is not None and now - stopped <= STATUS_WINDOW))
            if recent_only and not recent:
                continue
            events = live_sessions.events_since(directory / "events.jsonl", now - STATUS_WINDOW)
            summary = live_sessions.summarize(state, events, unit, now, state.get("config"))
            texts.append(live_sessions.status_text(summary, now, label))
        return texts

    # -------------------------------------------------------------- delivery

    def deliver(self, text: str, subject: dict) -> bool:
        """Send to every recipient, retrying the ones that failed for a passing reason.

        False only when told to stop before everyone had it.
        """
        pending: list[int] | None = None
        delay = 5.0
        while True:
            if pending is None:
                try:
                    pending = list(self.recipients(subject))
                except Exception as exc:
                    log.warning("cannot resolve recipients: %s", exc)
            if pending is not None:
                retry = []
                for chat in pending:
                    outcome = self.send(chat, text)
                    if outcome == RETRY:
                        retry.append(chat)
                    elif outcome == REFUSED:
                        log.info("chat %s refused a message; not retrying", chat)
                if not retry:
                    return True
                pending = retry
            if self.stopping():
                return False
            self.sleep(delay)
            delay = min(delay * 2, 300.0)
