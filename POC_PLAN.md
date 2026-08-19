# POC Plan — Telegram AI Trading Assistant (test mode)

Derived from `spec.txt`. Decisions taken: **OpenClaw** as agent runtime, **stub tools over
fixtures**, **dev API + CLI** as the pairing-link source.

## 0. Scope

**Definition of done for the POC**

> I run one command, get a `t.me/...` link, open it, and hold a useful trading conversation
> with an assistant that has a product identity, calls typed read-only tools, and refuses to
> invent statistics.

**In scope**

| Spec section | What the POC includes |
|---|---|
| §3.3 | Pairing link → Telegram identity bound to a user record |
| §7, §9 | Agent identity, system behaviour, evidence hierarchy A/B/C/D |
| §10 | Typed read-only tool namespaces `mt5.*`, `backtests.*` — **stubbed over fixtures** |
| §26 | Conversation memory (OpenClaw session state) |
| §49 | Tool allowlist: no shell, no filesystem, no arbitrary URLs |
| §53 | Minimal agent-run audit record |

**Out of scope (test mode)** — Stripe/billing (§34–38), website + auth (§3.1), real MT5
connectivity (§16–19), backtest engine (§12), pattern detectors (§14), event engine (§2),
proactive alerts (§30–32), multi-tenant isolation (§48).

**Target shape**

```text
cli/pair.py  ──►  Product Core (FastAPI + Postgres)
                        │  mints pairing token, prints link
                        │
   user opens t.me/<bot>│
                        ▼
                  OpenClaw Gateway ──► Anthropic (Sonnet 5)
                        │
                        ▼
                  Stub MCP server  ──► fixtures/*.json
                  mt5.* / backtests.*   (read-only, no execution)
```

---

## Phase 0 — Prerequisites (manual, ~20 min)

1. Create the bot: Telegram → `@BotFather` → `/newbot`. Record token and bot username.
   Set a description and `/setcommands` later; not needed now.
2. Anthropic API key with Sonnet 5 access.
3. Confirm local toolchain: Python 3.10, `uv`, Docker 28.1 + Compose v5.1, `psql`,
   `redis-cli`. **Node ≥22.22.3 is required** — the system node is v20.9.0 and
   OpenClaw hard-refuses to start on it. Node 24 is installed to `~/.local/n` and
   put on `PATH` per-process by `scripts/node-env.sh`; the global node is untouched.
4. Create `agents/.env` (gitignored from the first commit):
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `ANTHROPIC_API_KEY`, `DATABASE_URL`.

**Gate:** `.env` exists, is gitignored, and no token has ever been committed.

---

## Phase 1 — OpenClaw gateway talking to Telegram

Goal: any message in Telegram gets a Claude reply. No product behaviour yet.

1. `npm install` — OpenClaw is pinned in `package.json` and installed **locally**;
   the global npm prefix is root-owned and a global install would need sudo.
   npm 11 takes the install-script allowlist from the `allowScripts` field in
   `package.json`, not the `--allow-scripts` CLI flag.
2. `./scripts/install-config.sh` — installs the versioned config to
   `~/.openclaw/openclaw.json`, backing up any existing one.
3. Telegram channel and model config:

See `openclaw/openclaw.json5` for the committed config. Agents live under
   `agents.list` (an **array** of objects with `id`), **not** `agents.entries` —
   the published docs describe a shape this version rejects. Verified with
   `openclaw config schema` and `openclaw config validate`.

   Token comes from `TELEGRAM_BOT_TOKEN` in the environment, not the config file.
   `anthropic/claude-sonnet-5` confirmed present in `openclaw models list` (the
   `anthropic` provider only appears once `ANTHROPIC_API_KEY` is set).
4. `openclaw gateway`, DM the bot, then `openclaw pairing list telegram` and
   `openclaw pairing approve telegram <CODE>`.

**Gate:** a round trip in Telegram. Nothing product-specific yet — this is purely
"the plumbing is alive".

---

## Phase 2 — The pairing link (§3.3)

This is the headline deliverable and the one place OpenClaw does not fit the spec: its
Telegram pairing uses **numeric codes approved by the operator**, not `?start=<token>`
deep-link payloads. The POC therefore keeps token issuance in our Product Core and
automates the approval side.

1. **Postgres via Compose** — `docker-compose.yml` with one `postgres:16` service.
2. **Product Core** (`core/`, FastAPI + SQLAlchemy). Tables, all carrying `user_id` from
   day one per §48: `users`, `telegram_identities`, `pairing_tokens`, `agent_runs`.
3. **Token logic** (`core/pairing.py`): signed, single-use, 1-hour TTL — matching
   OpenClaw's own pairing-code expiry so the two halves cannot drift apart.
4. **Dev endpoint**: `POST /dev/pairing-token` → `{ "link": "https://t.me/<bot>?start=<token>" }`.
   No auth in test mode; bind to localhost only.
5. **Approver daemon** (`core/approver.py`): polls `openclaw pairing list telegram`,
   and when a pending code appears while an unconsumed token exists, runs
   `openclaw pairing approve telegram <CODE>`, then writes the `users` +
   `telegram_identities` rows and marks the token consumed. Refuse to approve when zero
   or more than one token is outstanding — never blanket-approve.
6. **CLI** (`cli/pair.py`): calls the endpoint, prints the link.
7. Bot's first reply on connect, per §3.3: *"Your Trading Assistant is connected."*

**Gate:** `python cli/pair.py` → link → open in Telegram → connected message within seconds,
with a `telegram_identities` row bound to a `user_id`.

**Known limitation to write down now:** the `?start=` payload is decorative here — OpenClaw
does not read it. This glue is exactly what the dedicated Telegram Adapter (§57) replaces at
Paid Alpha.

---

## Phase 3 — Product identity and behaviour (§9)

Turn a generic chatbot into the assistant the spec describes.

1. `prompts/system.md` — identity, and the §9.1 evidence hierarchy: Level A verified
   backtests, B deterministic account/market data, C external facts, D LLM reasoning;
   **never present D as A**.
2. Encode the hard behavioural rules:
   - §9.2 no invented backtests — must call `backtests.search()`, and say
     *"We don't currently have a validated backtest for this exact scenario"* when empty.
   - §9.3 conditional wording — "under the tested historical conditions", never
     "statistically proven".
   - §9.4 observation before interpretation — never "you are emotional".
   - §63 "nothing worth doing right now" is a valid, desirable answer.
3. Package as an OpenClaw skill at `~/.openclaw/workspace-trading/skills/trading-assistant/`
   and pin it via the agent's `skills: ["trading-assistant"]` allowlist (an explicit list
   **replaces** inherited defaults, so this is also a scope-control mechanism).
4. **Tool lockdown** (§49) — *already applied in Phase 1*, before the bot was ever reachable:

```json5
tools: {
  profile: "messaging",   // not "minimal": MCP tools only surface in coding/messaging
  deny: ["exec", "process", "write", "edit", "apply_patch",
         "browser", "sessions_spawn", "subagents"],
},
sandbox: { mode: "off", workspaceAccess: "none" },
```

**Gate:** ask "what does the backtest say about EURUSD M1 scalping?" with no tools wired yet
— it must decline rather than produce plausible numbers.

---

## Phase 4 — Stub tool layer (§10, §11)

Real tool schemas, fake data. This is what separates the POC from a themed chatbot.

1. **MCP server** (`mcp/server.py`), streamable-http, read-only:
   - `mt5.get_account()`, `mt5.get_positions()`, `mt5.get_trade_history()`
   - `backtests.search()`, `backtests.get_summary()`, `backtests.get_report()`
   - **Deliberately absent:** `mt5.open_trade` / `close_trade` / `modify_trade` (§10).
2. **Fixtures** (`mcp/fixtures/`): a realistic FTMO-style account, a couple of open
   positions, a short trade history containing one loss-escalation sequence. Seed
   `backtests.json` from the real artifacts already in `../trading/runs/*/summary.json`
   so `backtests.search()` returns genuine metrics in the §12 machine-readable shape
   (`pattern_id`, `version`, `sample_size`, `expectancy_r`, `detector_version`).
   Include **at least one deliberate gap** so the empty-result path is demonstrable.
3. Register: `openclaw mcp add trading --url http://127.0.0.1:8081/mcp --transport streamable-http`,
   verify with `openclaw mcp doctor trading --probe`.
4. Add the tool names to the agent's `tools.allow` so they survive the deny list.

**Gate:** the §66 conversation runs end-to-end — "how's my account?" returns fixture numbers;
"show me the backtest behind MZ-04" returns the stored report; "backtest for EURUSD M1"
returns nothing and the assistant says so plainly.

---

## Phase 5 — Prove it holds (§61)

1. **Deterministic tests**: pairing token is single-use and expires; approver refuses
   ambiguous cases; every MCP tool schema is read-only; no execution tool is reachable
   through the live agent.
2. **Behavioural eval set**: 12–15 scripted prompts as the seed of the §61 dataset, scored
   on — cited the correct backtest? invented statistics? overstated certainty? avoided
   false emotional diagnosis? called the right tools? Keep it as a file from day one; it is
   the artifact that survives every later rewrite.
3. **Cost visibility (§52)**: log model, tokens in/out and cost per turn into `agent_runs`.
   Cheap now, and "LLM cost per user" is required from the beginning.
4. **Error surface**: OpenClaw forwards raw provider errors straight into the Telegram
   chat — a billing failure reached the user as *"Your credit balance is too low to access
   the Anthropic API"*. Acceptable while developing, wrong for a paying user. Replace with
   a generic apology plus an internal alert before anyone but the founder is connected.
   `channels.telegram` exposes `errorPolicy` / `silentErrorReplies`; leave them alone until
   the eval set is green, because silencing errors during development hides real faults.

**Gate:** tests green, eval set passing, one recorded end-to-end demo.

---

## Optional stretch — outbound proof (§30)

A single `/brief` command assembling a §32-style morning brief deterministically from
fixtures, with the LLM only doing final summarisation. Proves the outbound path without
building the event engine.

---

## Repo layout (grows into §64)

```text
agents/
  docker-compose.yml
  .env                      # gitignored
  openclaw/openclaw.json5   # version-controlled copy of the gateway config
  core/                     # Product Core: app, db, models, pairing, approver
  mcp/                      # stub MCP server + fixtures/
  prompts/system.md
  skills/trading-assistant/ # symlinked into the OpenClaw workspace
  cli/pair.py
  tests/
```

---

## Risks and where OpenClaw bites

1. **Deep-link pairing is not native.** Numeric codes + operator approval; the approver
   daemon is glue that gets deleted at Paid Alpha. Accepted for the POC.
2. **Not a tenancy boundary.** OpenClaw's own docs state multi-user ownership features are
   usability features, not security boundaries, and that real isolation needs separate
   agents or gateways. §48 tenant isolation therefore **cannot** be demonstrated on this
   runtime — the POC is single-user by construction, and this is the trigger to replace
   the runtime, not a bug to fix later.
3. **Default tool surface is wide.** Shell/file/browser tools must be denied explicitly in
   Phase 3, before the bot is reachable from Telegram.
4. **Secrets.** Bot token and API key stay in env, never in the versioned config. Per §47,
   no MT5 credential of any kind enters the OpenClaw workspace — that constraint starts
   now, while there is nothing to leak.

## What survives the rewrite

Keep: the system prompt and skill, the tool schemas, the fixtures-as-contract, the eval set,
the Postgres schema, the pairing-token logic.
Discard: the OpenClaw gateway config and the approver daemon.

## Reuse available from `../trading/`

- `mt5-mcp-server/` — working MT5 MCP server on Wine Python; becomes the real `mt5.*`
  backing in the next milestone.
- `runs/*/summary.json` — real backtest output to seed the KB fixtures now.
- `signals/telegram.py` — stdlib Bot API sender, useful for the outbound path once the
  Telegram Adapter replaces OpenClaw.
