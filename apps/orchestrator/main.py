"""Run the orchestrator: poll Telegram, route by identity, answer, record.

    ./scripts/orchestrator.sh
"""

from __future__ import annotations

import logging
import sys

import agent
import config
import db
import identity
import llm as llm_mod
import telegram

log = logging.getLogger("orchestrator")

UNPAIRED = (
    "This assistant is invite-only. If you have a connection link, open it "
    "again — it carries the code that pairs this chat to your account."
)
BAD_TOKEN = (
    "That link is no longer valid — they are single-use and expire after an "
    "hour. Generate a fresh one and open it again."
)
WELCOME = (
    "Your Trading Assistant is connected.\n\n"
    "Ask me about your account, or what the backtests say about a setup. "
    "I won't invent numbers: if we don't have evidence for something, I'll "
    "tell you that instead."
)


def handle(msg: dict, provider) -> None:
    with db.session_scope() as s:
        user = identity.user_for_telegram(s, msg["telegram_user_id"])

        # Pairing: the only path from "a stranger messaged the bot" to a user.
        if user is None:
            if msg["start_token"]:
                user = identity.redeem(
                    s, msg["start_token"],
                    telegram_user_id=msg["telegram_user_id"],
                    chat_id=msg["chat_id"], username=msg["username"],
                    first_name=msg["first_name"])
                if user is None:
                    telegram.send(msg["chat_id"], BAD_TOKEN)
                    return
                log.info("paired telegram:%s -> user %s",
                         msg["telegram_user_id"], user.id)
                telegram.send(msg["chat_id"], WELCOME)
                return
            telegram.send(msg["chat_id"], UNPAIRED)
            return

        if msg["start_token"] is not None and not msg["text"].strip().removeprefix("/start").strip():
            telegram.send(msg["chat_id"], WELCOME)
            return
        if not msg["text"].strip():
            return

        reply = agent.run_turn(s, user, msg["text"], provider)
    telegram.send(msg["chat_id"], reply)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 1

    db.create_all()
    provider = llm_mod.build_provider()
    log.info("provider=%s model=%s mcp=%s", provider.name, provider.model, config.MCP_URL)
    log.info("polling telegram…")

    offset = None
    while True:
        updates, offset = telegram.poll(offset)
        for u in updates:
            msg = telegram.parse(u)
            if not msg or not msg["is_private"]:
                continue          # DM-only, like the OpenClaw config it replaces
            try:
                handle(msg, provider)
            except Exception:
                log.exception("failed handling update")
                telegram.send(msg["chat_id"],
                              "Something went wrong on my side handling that.")


if __name__ == "__main__":
    sys.exit(main())
