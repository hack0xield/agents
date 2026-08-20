"""LLM provider behind an interface (spec §8).

Two implementations:

    AnthropicProvider — the production path.
    StubProvider      — deterministic, no API key, no spend. Exists so the
                        adapter, identity, storage and audit paths can be
                        tested end to end without credits, and so a missing
                        key degrades into something obviously fake rather than
                        something plausible.
"""

from __future__ import annotations

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


class AnthropicProvider:
    def __init__(self, model: str = config.MODEL):
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        self.model = model
        self.name = "anthropic"

    def generate(self, system, messages, tools) -> Turn:
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=config.MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools or [],
                # §8: Sonnet 5 takes adaptive thinking; budget_tokens is
                # removed on this model and returns 400.
                thinking={"type": "adaptive"},
            )
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
    if config.LLM_PROVIDER == "stub":
        return StubProvider()
    return AnthropicProvider()
