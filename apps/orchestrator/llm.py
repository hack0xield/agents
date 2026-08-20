"""LLM provider behind an interface (spec §8).

Three implementations:

    AnthropicProvider — the production path.
    OllamaProvider    — a local model, free and offline. Ollama speaks the
                        Anthropic Messages API, so this is the same client
                        pointed at a different base URL, with the features
                        Ollama does not implement switched off.
    StubProvider      — deterministic, no model at all. Exists so the adapter,
                        identity, storage and audit paths can be tested
                        without credits, and so a missing key degrades into
                        something obviously fake rather than plausible.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import config


@dataclass
class Turn:
    """One provider round trip, with everything §53 wants recorded."""
    content: list[dict]                       # raw content blocks
    stop_reason: str | None = None
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None

    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content
                         if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLMProvider(Protocol):
    def generate(self, system: list[dict], messages: list[dict],
                 tools: list[dict]) -> Turn: ...


# Anthropic list price, USD per million tokens. Sonnet 5 is $3/$15 standard;
# the $2/$10 introductory rate ends 2026-08-31, which spec §46's cost model
# still assumes.
_PRICES = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}


def estimate_cost(model: str, turn: Turn) -> float | None:
    """USD for one turn, or None when the model is not priced.

    A local model costs nothing per token, so it records 0.0 rather than None —
    zero is a measurement, None means unknown.
    """
    if turn.provider == "ollama":
        return 0.0
    price = _PRICES.get(model)
    if not price:
        return None
    inp, out = price
    # Cache reads bill at ~0.1x, writes at ~1.25x.
    return round(
        (turn.input_tokens * inp
         + turn.cache_read_tokens * inp * 0.10
         + turn.cache_write_tokens * inp * 1.25
         + turn.output_tokens * out) / 1_000_000,
        6,
    )


class MessagesAPIProvider:
    """Anything speaking the Anthropic Messages API.

    Ollama implements it too (v0.14+), which is why this is parameterised
    rather than duplicated. `supports` gates the features a backend may not
    have: Ollama has no prompt caching and no extended thinking, and sending
    those fields anyway is at best ignored and at worst a 400.
    """

    def __init__(self, *, name: str, model: str, base_url: str | None = None,
                 api_key: str | None = None, supports: set[str] | None = None,
                 free: bool = False):
        import anthropic
        self.client = anthropic.Anthropic(
            api_key=api_key or "unused", base_url=base_url,
            timeout=float(os.environ.get("LLM_TIMEOUT", "300")),
        )
        self.name = name
        self.model = model
        self.supports = supports if supports is not None else {"thinking", "cache"}
        self.free = free

    def _system(self, system: list[dict]) -> list[dict]:
        if "cache" in self.supports:
            return system
        # Strip cache_control rather than dropping the block: the text still
        # has to be sent, it just will not be cached.
        return [{k: v for k, v in b.items() if k != "cache_control"} for b in system]

    def generate(self, system, messages, tools) -> Turn:
        kwargs = dict(
            model=self.model, max_tokens=config.MAX_TOKENS,
            system=self._system(system), messages=messages, tools=tools or [],
        )
        if "thinking" in self.supports:
            kwargs["thinking"] = {"type": "adaptive"}

        t0 = time.perf_counter()
        try:
            resp = self.client.messages.create(**kwargs)
        except Exception as e:
            return Turn(content=[], provider=self.name, model=self.model,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        error=f"{type(e).__name__}: {e}")

        u = resp.usage
        return Turn(
            content=[b.model_dump() for b in resp.content],
            stop_reason=resp.stop_reason,
            model=resp.model,
            provider=self.name,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )


def AnthropicProvider(model: str = config.MODEL):
    return MessagesAPIProvider(
        name="anthropic", model=model, api_key=config.ANTHROPIC_API_KEY or None,
    )


def OllamaProvider(model: str | None = None):
    """A local model. Free, offline, and no prompt caching or thinking."""
    return MessagesAPIProvider(
        name="ollama",
        model=model or config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        api_key="ollama",              # required by the endpoint, not checked
        supports=set(),                # no caching, no extended thinking
        free=True,
    )


class StubProvider:
    """Answers without a model. Deliberately unmistakable: it must never be
    confused for a real reply in a transcript someone reads later."""

    def __init__(self, model: str = "stub"):
        self.model = model
        self.name = "stub"

    def generate(self, system, messages, tools) -> Turn:
        last = ""
        for m in reversed(messages):
            if m["role"] == "user":
                c = m["content"]
                last = c if isinstance(c, str) else next(
                    (b.get("text", "") for b in c if b.get("type") == "text"), "")
                break
        return Turn(
            content=[{"type": "text", "text":
                      f"[STUB PROVIDER — no model was called] "
                      f"{len(tools)} tools available. You said: {last[:160]}"}],
            stop_reason="end_turn", provider=self.name, model=self.model,
            input_tokens=0, output_tokens=0, latency_ms=1,
        )


def build_provider() -> LLMProvider:
    choice = config.LLM_PROVIDER
    if choice == "stub":
        return StubProvider()
    if choice == "ollama":
        return OllamaProvider()
    return AnthropicProvider()
