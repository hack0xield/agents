"""Who is talking, and what they are allowed to see.

Identity comes from the Telegram sender id, which Telegram authenticated. It
is never taken from message content, and never from anything the model said.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

PAIRING_TTL = timedelta(hours=1)


def mint_pairing_token(s: Session, *, display_name: str | None = None,
                       email: str | None = None) -> tuple[models.User, str]:
    """Create a user and a single-use token for the t.me deep link (§3.3)."""
    user = models.User(display_name=display_name, email=email)
    s.add(user)
    s.flush()
    token = secrets.token_urlsafe(24)
    s.add(models.PairingToken(
        token=token, user_id=user.id,
        expires_at=datetime.now(timezone.utc) + PAIRING_TTL,
    ))
    return user, token


def redeem(s: Session, token: str, *, telegram_user_id: int, chat_id: int,
           username: str | None, first_name: str | None) -> models.User | None:
    """Bind a Telegram sender to the user the token was minted for.

    Single use and time limited. A token that has been consumed is not an
    error to retry — it is a token someone else may be replaying.
    """
    row = s.get(models.PairingToken, token)
    if row is None or row.consumed_at is not None:
        return None
    if row.expires_at < datetime.now(timezone.utc):
        return None

    existing = s.scalar(select(models.TelegramIdentity).where(
        models.TelegramIdentity.telegram_user_id == telegram_user_id))
    if existing is not None:
        return None       # already bound; pairing again would move the account

    s.add(models.TelegramIdentity(
        user_id=row.user_id, telegram_user_id=telegram_user_id, chat_id=chat_id,
        username=username, first_name=first_name,
    ))
    row.consumed_at = datetime.now(timezone.utc)
    return s.get(models.User, row.user_id)


def user_for_telegram(s: Session, telegram_user_id: int) -> models.User | None:
    ident = s.scalar(select(models.TelegramIdentity).where(
        models.TelegramIdentity.telegram_user_id == telegram_user_id))
    if ident is None:
        return None
    user = s.get(models.User, ident.user_id)
    return None if (user is None or user.disabled) else user


def accounts_for(s: Session, user_id) -> list[dict]:
    rows = s.scalars(select(models.TradingAccount).where(
        models.TradingAccount.user_id == user_id)).all()
    return [{"id": str(a.id), "nickname": a.nickname, "login": a.login,
             "server": a.server, "access": a.access,
             "credential_ref": a.credential_ref,
             "connection_state": a.connection_state, "is_default": a.is_default}
            for a in rows]


def tool_scope(s: Session, user: models.User) -> dict:
    """Server-owned arguments forced into every tool call.

    Currently the default account id. This is the value the model is not
    allowed to choose — see tools.USER_SCOPED_ARGS.
    """
    accounts = accounts_for(s, user.id)
    default = next((a for a in accounts if a["is_default"]), None) or (
        accounts[0] if accounts else None)
    if not default:
        # No account: the tools receive no ref and answer NO_ACCOUNT. They must
        # never inherit whichever account the terminal is currently on.
        return {}
    return {"account_id": default["id"],
            "credential_ref": default["credential_ref"]}
