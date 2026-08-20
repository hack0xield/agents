"""MCP tools, bound to a user by the server rather than by the model.

This module is the reason the orchestrator exists. Under OpenClaw the tool
server had exactly one client — the gateway — so it could not tell users
apart, and the only way to scope data per user would have been an argument the
model filled in. A parameter an LLM supplies is not an access control.

Here the caller is known from the Telegram sender id, and any user-scoped
argument is overwritten from the database *after* the model has spoken. The
model can ask for whatever it likes; it cannot choose whose data it gets.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import config

# Arguments the server owns. If the model emits one of these, the value is
# replaced — never merged, never trusted.
USER_SCOPED_ARGS = {"account_id", "user_id", "chat_id", "telegram_chat_id",
                    "credential_ref"}


@dataclass
class ToolResult:
    text: str
    is_error: bool
    duration_ms: int


async def _with_session(fn):
    async with streamable_http_client(config.MCP_URL) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def list_tools() -> list[dict]:
    """MCP tool definitions, converted to the Anthropic tool schema."""
    async def _go(s):
        return (await s.list_tools()).tools

    try:
        tools = asyncio.run(_with_session(_go))
    except Exception:
        return []
    return [
        {
            "name": t.name.replace(".", "__"),   # Anthropic tool names: no dots
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


def call_tool(name: str, arguments: dict, *, scope: dict[str, Any]) -> ToolResult:
    """Invoke one tool with server-owned arguments forced in.

    `scope` carries the identity-derived values. It wins over anything the
    model supplied, which is the entire security property of this function.
    """
    args = dict(arguments or {})
    for k in USER_SCOPED_ARGS:
        args.pop(k, None)
    args.update({k: v for k, v in scope.items() if v is not None})

    mcp_name = name.replace("__", ".", 1) if "__" in name else name

    async def _go(s):
        return await s.call_tool(mcp_name, args)

    t0 = time.perf_counter()
    try:
        res = asyncio.run(_with_session(_go))
    except Exception as e:
        return ToolResult(f"tool transport error: {type(e).__name__}: {e}", True,
                          int((time.perf_counter() - t0) * 1000))
    text = ""
    if res.content:
        text = getattr(res.content[0], "text", "") or ""
    return ToolResult(text, bool(res.is_error), int((time.perf_counter() - t0) * 1000))


def summarize(text: str, limit: int = 120) -> str:
    """Short, loggable description of a result, for the tool_calls table."""
    t = text.strip()
    if t.startswith("[") or t.startswith("{"):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                return f"{len(v)} row(s)"
            if isinstance(v, dict):
                for k in ("found", "sent", "ok", "connection_state"):
                    if k in v:
                        return f"{k}={v[k]}"
                return f"{len(v)} field(s)"
        except ValueError:
            pass
    return t[:limit]
