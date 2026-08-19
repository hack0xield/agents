# Replacing OpenClaw with our own orchestrator

Spec §6 says OpenClaw is fine for the User #1 prototype but "must not be a
mandatory architectural dependency". This is what the replacement is, when to do
it, and what it costs.

Written 2026-08-20, after building the POC on OpenClaw. Every item in "the case"
below is something that actually happened during that build, not a prediction.

---

## The case, from evidence

| Problem | Measured | Spec |
|---|---|---|
| ~29k tokens of base prompt on every turn, no lever | 33.9k total, ours is 4.5k | §43 |
| Tool calls invisible: trajectory export shows **0** tool events under `claude-cli` | verified | §53 |
| Token accounting broken — a multi-hundred-token reply reported `input: 2, output: 2` | verified | §52 |
| One session per DM; CLI and Telegram share `:main` | eval runs landed in the founder's chat | §48 |
| 65 operator commands advertised, incl. `/export_session` (dumps the system prompt) | contained, not fixed — `commands.text` still routes them | §49 |
| Any local process can drive the gateway (loopback pairing auto-approved) | CLI works with the token unset | §48 |
| Not a tenancy boundary — OpenClaw's own docs say so | — | §48 |
| Pairing is numeric codes + operator approval, not `?start=` deep links | §3.3 not implementable as written | §3.3 |
| Config rewritten at runtime; our file is not the source of truth | needed `config patch`, not `cp` | — |
| 17 unrelated skills inherited by default | fixed with `skills: []`, but the default is wrong for a product | §41 |

None of these are bugs in OpenClaw. It is a personal assistant for one trusted
operator and it is good at that. They are all the same mismatch: we are building
a multi-tenant product on a single-operator runtime.

**The trigger is user #2.** Everything above is survivable while the founder is
the only user. None of it is survivable with a paying customer, and three items
(session sharing, operator commands, gateway trust) are outright unsafe.

---

## What it is

A few hundred lines. The agent loop is genuinely small; the work is everything
around it.

```python
# core/agent.py — sketch, not the finished thing
import anthropic
from anthropic.lib.tools.mcp import async_mcp_tool

client = anthropic.AsyncAnthropic()

async def run_turn(user_id: str, text: str, mcp_client) -> str:
    system = [{
        "type": "text",
        "text": build_system_prompt(user_id),   # SOUL.md + trader profile
        "cache_control": {"type": "ephemeral"}, # §43: stable prefix cached
    }]

    tools = [async_mcp_tool(t, mcp_client)
             for t in (await mcp_client.list_tools()).tools]

    messages = load_context(user_id)            # §26: summary + recent turns
    messages.append({"role": "user", "content": text})

    runner = client.beta.messages.tool_runner(
        model="claude-sonnet-5",                # §8: Sonnet is the normal brain
        max_tokens=16000,
        system=system,
        tools=tools,
        messages=messages,
    )

    async for message in runner:
        record_agent_run(user_id, message)      # §53: model, tokens, cost, tools
    return final_text(message)
```

Three things to notice:

**Our MCP server plugs in unchanged.** `async_mcp_tool` converts MCP tool
definitions to Anthropic tool definitions. `mcp_server/` — all seven tools, the
schemas, the descriptions the model reads — moves across untouched.

**The system prompt is ours.** That is the 29k saving: we send `SOUL.md` and the
trader's profile, and nothing else. Same behaviour, a fraction of the context.

**`record_agent_run` is the audit trail** that cannot exist today. `usage` gives
`input_tokens`, `output_tokens`, `cache_read_input_tokens` and
`cache_creation_input_tokens` per call — §52's "LLM cost per paying user"
becomes a database column instead of a wish.

---

## What survives, and what does not

**Survives — most of the work is already done:**

- `mcp_server/` — tools, schemas, the live runs reader, artifact serving
- `agent-workspace/SOUL.md` — becomes the system prompt directly
- `agent-workspace/TOOLS.md` — merges into the system prompt
- `tests/agent-evals/behaviour.md` — the eval set is the safety net *for* the
  migration; run it against both runtimes and compare
- `tests/smoke-mcp.sh` — unchanged, the tool server does not move

**Discarded:**

- `openclaw/openclaw.json5` and every workaround in it
- `scripts/install-config.sh`, `install-agent-workspace.sh`, `link-claude-cli.sh`
- `mcp_server/tool-calls.jsonl` — superseded by the real audit trail
- the pairing-approver design sketched in POC_PLAN.md Phase 2

---

## What has to be built

1. **Telegram adapter** — webhook (spec §3.3 wants webhooks, not polling) with a
   signed-secret check, plus the `?start=<token>` deep-link flow OpenClaw cannot
   do. This is where §3.3 becomes implementable.
2. **Session store** — Postgres, keyed by `user_id`, not one global `:main`.
   Fixes the isolation problem structurally rather than by configuration.
3. **Context assembly** — §26: rolling summary + recent turns + retrieved
   memories. The API's compaction beta (`compact_20260112`) or context editing
   (`clear_tool_uses_20250919`) can do the heavy lifting.
4. **`agent_runs` / `tool_calls` tables** — §53.
5. **Model routing** — §8: Haiku for classification and summarisation, Sonnet for
   conversation, Opus as the exception. One function, real savings.
6. **Delivery** — retries, chunking at Telegram's 4096 limit, error messages that
   do not leak provider internals (the billing error that reached the chat).

Items 1, 2 and 6 are the actual work. The loop is the easy part.

---

## Pricing — check this before reusing spec §46's numbers

Spec §46 computes ~$1,350/month for 1,000 users using **$2 / $10** per MTok for
Sonnet 5. That is **introductory pricing that ends 2026-08-31**. Standard is
**$3 / $15** — 50% higher.

| Model | Input $/MTok | Output $/MTok | Context |
|---|---|---|---|
| Sonnet 5 | $3.00 ($2.00 intro to 2026-08-31) | $15.00 ($10.00 intro) | 1M |
| Haiku 4.5 | $1.00 | $5.00 | 200K |
| Opus 5 | $5.00 | $25.00 | 1M |

Re-running §46 at standard pricing: proactive messaging goes ~$1,350 → ~$2,025/mo
at 1,000 users. Still ~4% of revenue at £39/user, so the conclusion does not
change — but the number in the spec is optimistic by half and should be updated.

Cache reads bill at ~0.1x and cache writes at ~1.25x, which is why the stable
system prefix belongs behind a `cache_control` breakpoint.

---

## API notes that differ from older patterns

Relevant if any code is written from memory rather than current docs:

- **`budget_tokens` is removed on Sonnet 5** — returns 400. Use
  `thinking: {"type": "adaptive"}` and `output_config: {"effort": ...}`.
- **Assistant prefill is removed** — 400 on Sonnet 5. Use structured outputs or
  system-prompt instruction instead.
- **`output_config: {"format": ...}`**, not the deprecated `output_format`.
- Parse tool inputs with `json.loads`, never string-match the serialized input.
- Return *all* `tool_result` blocks in a single user message; splitting them
  trains the model out of parallel tool calls.

---

## Effort

The loop and tool plumbing is a day, because the tools already exist and convert
directly. The Telegram adapter, session store, context assembly and audit tables
are the real scope — call it one to two focused weeks to reach parity with what
runs today, plus the eval set green on both runtimes before switching over.

Worth doing when the answer to "who else is using this" stops being "nobody".
