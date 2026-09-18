"""Chat commands, handled without a model.

Spec §45: do not spend a model on templated output. These are also the
commands a model must not *pretend* to run — /reset asked of the LLM produced
"Let's start fresh" while the conversation history stayed exactly where it was.
A command that reports success must have done something.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
import identity
import models

HELP = """I'm a trading assistant. I work from backtests we've actually run and
your own account data — I won't invent numbers.

Ask me things like:
  • what backtests do we have stored?
  • what does the evidence say about XAUUSD?
  • send me the files for that run
  • is the live trader running? start it / stop it

Commands:
  /help     this message
  /reset    start a fresh conversation (history is kept, not shown)
  /status   what I'm connected to
  /whoami   which account you're paired to"""


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "never"


def handle(s: Session, user: models.User, text: str) -> str | None:
    """Return a reply if this was a command, or None to pass to the model."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    word = stripped[1:].split()[0].lower() if len(stripped) > 1 else ""

    if word in ("help", "start", "commands"):
        return HELP

    if word in ("reset", "new", "clear"):
        # A new conversation row. The old one is kept — deleting a trader's
        # history because they asked for a clean slate is not the same request.
        conv = s.scalar(select(models.Conversation).where(
            models.Conversation.user_id == user.id).order_by(
            models.Conversation.updated_at.desc()))
        kept = 0
        if conv is not None:
            kept = s.scalar(select(func.count(models.Message.id)).where(
                models.Message.conversation_id == conv.id)) or 0
        s.add(models.Conversation(user_id=user.id))
        return (f"Fresh conversation started. {kept} earlier message(s) are "
                f"kept on file but I won't refer to them.")

    if word == "status":
        accounts = identity.accounts_for(s, user.id)
        runs = s.scalar(select(func.count(models.AgentRun.id)).where(
            models.AgentRun.user_id == user.id)) or 0
        lines = [f"Model: {config.LLM_PROVIDER} / "
                 f"{config.OLLAMA_MODEL if config.LLM_PROVIDER == 'ollama' else config.MODEL}",
                 f"Turns so far: {runs}"]
        if accounts:
            for a in accounts:
                lines.append(f"Account: {a['nickname']} — login {a['login']} "
                             f"on {a['server']} ({a['access']})")
        else:
            lines.append("Account: none connected — I can't see any trading data.")
        return "\n".join(lines)

    if word == "whoami":
        ident = s.scalar(select(models.TelegramIdentity).where(
            models.TelegramIdentity.user_id == user.id))
        return (f"{user.display_name or 'unnamed'} · {user.subscription_status}\n"
                f"Telegram id {ident.telegram_user_id if ident else '?'}\n"
                f"Paired {_fmt(ident.paired_at if ident else None)}")

    # An unknown slash command is not a question. Answering it conversationally
    # is how "/reset" got a cheerful "starting fresh" that reset nothing.
    return f"I don't know the command /{word}. Try /help."
