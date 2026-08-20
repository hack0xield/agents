"""One conversational turn: context in, reply out, everything recorded."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import identity
import llm as llm_mod
import models
import prompt as prompt_mod
import tools as tools_mod

MAX_TOOL_ITERATIONS = 8       # a runaway loop is a cost incident, not a feature


def _conversation(s: Session, user: models.User) -> models.Conversation:
    conv = s.scalar(select(models.Conversation).where(
        models.Conversation.user_id == user.id).order_by(
        models.Conversation.updated_at.desc()))
    if conv is None:
        conv = models.Conversation(user_id=user.id)
        s.add(conv)
        s.flush()
    return conv


def _history(s: Session, conv: models.Conversation) -> list[dict]:
    rows = s.scalars(select(models.Message).where(
        models.Message.conversation_id == conv.id).order_by(
        models.Message.created_at.desc()).limit(config.HISTORY_TURNS)).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _system(user: models.User, accounts: list[dict], profile_md: str | None) -> list[dict]:
    """Stable prefix first so it caches (§43); per-user block after it."""
    return [
        {"type": "text", "text": prompt_mod.base_prompt(),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text",
         "text": prompt_mod.user_context(user.display_name, profile_md, accounts)},
    ]


def run_turn(s: Session, user: models.User, text: str,
             provider: llm_mod.LLMProvider) -> str:
    conv = _conversation(s, user)
    accounts = identity.accounts_for(s, user.id)
    scope = identity.tool_scope(s, user)
    profile_md = (config.WORKSPACE / "USER.md").read_text() if (
        config.WORKSPACE / "USER.md").is_file() else None

    messages = _history(s, conv)
    messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    s.add(models.Message(conversation_id=conv.id, user_id=user.id,
                         role="user", content=[{"type": "text", "text": text}]))

    tool_defs = tools_mod.list_tools()
    system = _system(user, accounts, profile_md)
    reply = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        turn = provider.generate(system, messages, tool_defs)

        run = models.AgentRun(
            user_id=user.id, conversation_id=conv.id,
            provider=turn.provider, model=turn.model,
            prompt_version=config.PROMPT_VERSION,
            input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
            cache_read_tokens=turn.cache_read_tokens,
            cache_write_tokens=turn.cache_write_tokens,
            cost_usd=llm_mod.estimate_cost(turn.model, turn),
            latency_ms=turn.latency_ms, stop_reason=turn.stop_reason,
            error=turn.error,
        )
        s.add(run)
        s.flush()

        if turn.error:
            # The provider failed. Say so without pasting its internals into
            # the chat — a billing message reached a user that way once.
            return ("I can't reach the model right now. That's on my side, not "
                    "yours — try again shortly.")

        s.add(models.Message(conversation_id=conv.id, user_id=user.id,
                             role="assistant", content=turn.content))
        messages.append({"role": "assistant", "content": turn.content})
        reply = turn.text() or reply

        uses = turn.tool_uses()
        if not uses:
            break

        # All results go back in ONE user message; splitting them trains the
        # model out of parallel tool calls.
        results = []
        for u in uses:
            res = tools_mod.call_tool(u["name"], u.get("input") or {}, scope=scope)
            s.add(models.ToolCall(
                agent_run_id=run.id, user_id=user.id, tool_name=u["name"],
                arguments=u.get("input") or {},
                result_summary=tools_mod.summarize(res.text),
                is_error=res.is_error, duration_ms=res.duration_ms,
            ))
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": res.text[:20000], "is_error": res.is_error})
        s.add(models.Message(conversation_id=conv.id, user_id=user.id,
                             role="user", content=results))
        messages.append({"role": "user", "content": results})

    conv.updated_at = datetime.now(timezone.utc)
    return reply or "I don't have anything useful to add there."
