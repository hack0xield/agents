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


def _est_tokens(content) -> int:
    """Rough token count. Deliberately an estimate, not a tokenizer call.

    Four characters per token is close enough for a budget whose job is to stay
    well clear of the context limit, and it costs nothing. Being wrong by 20%
    here changes how much history survives, not whether the request succeeds.
    """
    return len(json.dumps(content, default=str)) // 4


def _has_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _stub_tool_results(content) -> list:
    """Keep the block, drop the payload.

    The tool_use_id has to survive: removing the block entirely would orphan
    the assistant tool_use it answers, and the API rejects that. What goes is
    the data, which is both the expensive part and the part that has since
    gone stale.
    """
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            b = {**b, "content": "[earlier result — superseded, ask again if needed]"}
        out.append(b)
    return out


def _history(s: Session, conv: models.Conversation) -> list[dict]:
    """Recent conversation, newest-first budgeted, oldest dropped.

    Three rules, in order of importance:

    1. The sequence must start clean. A `tool_result` whose matching
       `tool_use` was trimmed away is a hard 400, and it would surface as an
       intermittent failure that depends on where the boundary happened to
       fall.
    2. Conversation text is cheap and is what people mean by "remember what I
       said". It survives as long as the budget allows.
    3. Tool payloads are expensive and perishable. Only the newest few keep
       their contents.
    """
    msgs: list[dict] = []
    budget = config.HISTORY_TOKEN_BUDGET
    seen_tool_results = 0
    offset = 0
    spent = False

    # Newest first, in chunks: what gets dropped when the budget runs out is
    # always the oldest thing present, and a conversation with thousands of
    # rows is never fully loaded to build a window that will not hold it.
    while not spent and offset < config.HISTORY_MAX_ROWS:
        rows = s.scalars(select(models.Message).where(
            models.Message.conversation_id == conv.id).order_by(
            models.Message.created_at.desc())
            .limit(config.HISTORY_CHUNK_ROWS).offset(offset)).all()
        if not rows:
            break
        offset += len(rows)

        for m in rows:
            content = m.content
            if _has_tool_result(content):
                seen_tool_results += 1
                if seen_tool_results > config.TOOL_RESULTS_KEPT_FULL:
                    content = _stub_tool_results(content)

            cost = _est_tokens(content)
            if msgs and cost > budget:
                spent = True
                break
            budget -= cost
            msgs.append({"role": m.role, "content": content})

    msgs.reverse()

    # Rule 1. Anthropic wants the first message to be a user turn, and a user
    # turn carrying tool_result blocks needs the assistant tool_use before it —
    # which, at the front of a trimmed window, is exactly what is missing.
    while msgs and (msgs[0]["role"] != "user" or _has_tool_result(msgs[0]["content"])):
        msgs.pop(0)

    return msgs


def _mark_cache_breakpoint(messages: list[dict]) -> None:
    """Cache everything up to the end of the last completed turn (§43).

    Without this the whole history is re-read at full input price on every
    turn, which is what made a small window look like the cheap option. Builds
    new dicts rather than mutating: `content` is still the JSON the ORM loaded,
    and editing it in place would mark the row dirty and write it back.
    """
    if not messages:
        return
    content = messages[-1].get("content")
    if not (isinstance(content, list) and content and isinstance(content[-1], dict)):
        return
    last = {**content[-1], "cache_control": {"type": "ephemeral"}}
    messages[-1] = {**messages[-1], "content": [*content[:-1], last]}


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
    _mark_cache_breakpoint(messages)
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
            # the chat — a billing message reached a user that way once. But a
            # quota that resets is worth distinguishing from a broken service,
            # because the useful response differs: wait, versus tell someone.
            low = turn.error.lower()
            if any(w in low for w in ("rate limit", "429", "quota", "exhausted",
                                      "too many requests")):
                return ("I've hit my usage limit for now. It resets on a timer — "
                        "try again in a while.")
            if any(w in low for w in ("credit", "billing", "payment", "402")):
                return ("My account needs topping up before I can answer. "
                        "Nothing wrong on your side.")
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
