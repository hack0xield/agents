"""Telegram adapter — the only place a sender becomes a user.

Long polling for development. Spec §3.3 wants webhooks in production; the
difference is confined to `poll()` and does not reach anything below it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

import config

API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, params: dict) -> dict:
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                    timeout=70) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_FENCE = re.compile(r"```[a-zA-Z0-9+-]*\n?")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def plain(text: str) -> str:
    """Strip markdown the transport cannot render.

    sendMessage is called without parse_mode, so markup arrives as literal
    characters. For emphasis that is merely ugly, but a backtick against a URL
    breaks it: Telegram's link detector reads the trailing character as part of
    the address, so `http://host/chart.html` is delivered as a link to
    /chart.html%60 and 404s. The model has no way to know that, and asking it
    to remember would not survive the next model, so it is removed here.

    Not a markdown renderer — just the three forms that actually turn up.
    """
    text = _FENCE.sub("", text)
    text = text.replace("`", "")
    return _BOLD.sub(r"\1", text)


def send(chat_id: int, text: str) -> bool:
    """Telegram caps a message at 4096 characters, so long replies are split
    rather than truncated — losing the end of an explanation is worse than
    sending two messages."""
    text = plain(text)
    ok = True
    for chunk in [text[i:i + 3900] for i in range(0, max(len(text), 1), 3900)] or [""]:
        r = _call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                  "disable_web_page_preview": "true"})
        ok = ok and bool(r.get("ok"))
    return ok


SENT, RETRY, REFUSED = "sent", "retry", "refused"


def deliver(chat_id: int, text: str) -> str:
    """Send like `send`, and say whether a failure is worth trying again.

    REFUSED is Telegram's own 400 or 403 — the chat is gone, or the user
    blocked the bot — which no retry will change. Anything else that fails,
    a network error, a rate limit, Telegram's own trouble, is RETRY.
    """
    text = plain(text)
    for chunk in [text[i:i + 3900] for i in range(0, max(len(text), 1), 3900)] or [""]:
        r = _call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                  "disable_web_page_preview": "true"})
        if not r.get("ok"):
            return REFUSED if r.get("error_code") in (400, 403) else RETRY
    return SENT


def poll(offset: int | None, timeout: int = 50) -> tuple[list[dict], int | None]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = _call("getUpdates", params)
    if not r.get("ok"):
        time.sleep(3)          # transient network or 409; do not hot-loop
        return [], offset
    updates = r.get("result", [])
    if updates:
        offset = updates[-1]["update_id"] + 1
    return updates, offset


def parse(update: dict) -> dict | None:
    """Flatten an update into what the router needs, or None to ignore it."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    frm, chat = msg.get("from") or {}, msg.get("chat") or {}
    text = msg.get("text") or ""
    if not frm.get("id") or not chat.get("id"):
        return None
    start_token = None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        start_token = parts[1].strip() if len(parts) > 1 else ""
    return {
        "telegram_user_id": int(frm["id"]),
        "chat_id": int(chat["id"]),
        "username": frm.get("username"),
        "first_name": frm.get("first_name"),
        "text": text,
        "start_token": start_token,
        "is_private": chat.get("type") == "private",
    }
