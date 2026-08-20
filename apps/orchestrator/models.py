"""Domain model for the orchestrator (spec §50).

Every row that belongs to a person carries `user_id`. That is the whole point
of this layer: on OpenClaw there was no notion of who was asking, so per-user
data could only be scoped by a parameter the model itself supplied — which is
not an access control. Here identity comes from the Telegram adapter and is
never model-supplied.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    # §54: the founder is simply marked INTERNAL. No billing needed to prove
    # the product, so subscription state is a column, not a Stripe dependency.
    subscription_status: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    telegram_identities: Mapped[list[TelegramIdentity]] = relationship(back_populates="user")
    trading_accounts: Mapped[list[TradingAccount]] = relationship(back_populates="user")


class TelegramIdentity(Base):
    """The binding that makes identity real: a Telegram sender id we trust
    because Telegram authenticated it, not because a message claimed it."""

    __tablename__ = "telegram_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(64))
    paired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="telegram_identities")


class PairingToken(Base):
    """Single-use, expiring token behind the t.me deep link (spec §3.3)."""

    __tablename__ = "pairing_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradingAccount(Base):
    """An MT5 account belonging to one user.

    No password field, by design. Credentials live in a secrets store keyed by
    `credential_ref`; spec §47 forbids them in Postgres, and this table is read
    by code paths that build LLM context.
    """

    __tablename__ = "trading_accounts"
    __table_args__ = (UniqueConstraint("user_id", "login", "server", name="uq_account_per_user"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    nickname: Mapped[str] = mapped_column(String(120))
    login: Mapped[int] = mapped_column(BigInteger)
    server: Mapped[str] = mapped_column(String(120))
    broker: Mapped[str | None] = mapped_column(String(120))
    credential_ref: Mapped[str | None] = mapped_column(String(200))
    access: Mapped[str] = mapped_column(String(32), default="investor_read_only")
    connection_state: Mapped[str] = mapped_column(String(32), default="DISCONNECTED")
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="trading_accounts")


class Conversation(Base):
    """One conversation per user. Replaces OpenClaw's single shared `:main`
    session, which mixed the CLI and the founder's Telegram chat together."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    summary: Mapped[str | None] = mapped_column(Text)          # §26 rolling summary
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conv_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict] = mapped_column(JSONB)   # full block list, not just text
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentRun(Base):
    """One LLM turn (spec §53). This is what OpenClaw could not give us:
    tokens and cost per user, which §52 needs from the beginning."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ToolCall(Base):
    """Every tool invocation, attributed to a user and a run (spec §53)."""

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments: Mapped[dict | None] = mapped_column(JSONB)
    result_summary: Mapped[str | None] = mapped_column(Text)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
