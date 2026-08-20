# Running the system

## The shape of it

```text
        Telegram
            │  (long polling)
            ▼
   ┌──────────────────┐        the app: identity, sessions,
   │   orchestrator   │        the agent loop, the audit trail
   └────┬────────┬────┘
        │        │
        │        └──────────────► Anthropic API   (or LLM_PROVIDER=stub)
        │
        ▼  MCP over :8081
   ┌──────────────────┐        11 tools: backtests.* and mt5.*
   │    mcp-server    │        backtests.* read ../trading/runs directly
   └────────┬─────────┘
            │  HTTP :8082
            ▼
   ┌──────────────────┐        the only thing that talks to MT5,
   │    mt5-bridge    │        runs under Wine
   └────────┬─────────┘
            ▼
      MT5 terminal ──► broker

   ┌──────────────────┐
   │    postgres      │  ◄── orchestrator only
   └──────────────────┘        users, accounts, conversations, agent_runs
```

Four long-running processes. Two are optional: without the bridge, `mt5.*`
reports DISCONNECTED and everything else still works; without Postgres, nothing
works.

---

## Start order, and why

Start bottom-up. Each layer only needs the one beneath it, and starting out of
order gives confusing failures rather than clean ones.

```bash
cd ~/Documents/trading_assistant/agents

# 1. database — the orchestrator will not start without it
docker compose up -d

# 2. MT5 bridge (terminal 1) — launches the MT5 terminal if it is not running
./scripts/mt5-bridge.sh

# 3. tools (terminal 2) — calls the bridge, so start it after
./scripts/mcp-server.sh

# 4. the app (terminal 3)
LLM_PROVIDER=stub ./scripts/orchestrator.sh
```

Order matters in one direction only: the MCP server resolves tools lazily, so
if the bridge is down its `mt5.*` calls fail at request time rather than at
startup — which looks like the assistant *choosing* not to answer.

**Do not run the OpenClaw gateway at the same time.** It polls the same bot
token; Telegram hands each update to whoever asks first, so the two silently
steal each other's messages. It is superseded — see the note at the end.

Check everything is up:

```bash
curl -s localhost:8082/health      # {"ok": true, "refs": [...]}
./tests/smoke-mcp.sh               # 11 tools green
```

---

## One-time setup, per user

Starting the processes does not create a user. Two steps, in order:

```bash
# 1. create a user and a pairing link
./scripts/pair.py "Eduard"
#    user   6f1c…-…
#    link   https://t.me/ptrading_assistant_bot?start=Xq7…
```

Open the link in Telegram and press Start. That binds *that* Telegram account
to *that* user row, and is the only way a sender becomes someone the assistant
will talk to.

```bash
# 2. connect an MT5 account to that user (optional)
./scripts/connect-account.py --user 6f1c…-… \
    --login 110119104 --server MetaQuotes-Demo --nickname "Demo" --allow-master
```

Without step 2 the assistant works, and `mt5.*` answers `NO_ACCOUNT` — which is
correct, not broken. It will never show a user an account that is not theirs.

The bridge notices a new account without restarting.

---

## Then just use it

Message the bot. Ask about backtests, ask it to run one, ask about your account.

To see what happened:

```bash
tail -f mcp_server/tool-calls.jsonl        # tool calls as they happen

docker compose exec db psql -U trading -d trading_assistant -c "
select u.display_name, r.model, r.input_tokens, r.output_tokens, r.cost_usd
from agent_runs r join users u on u.id = r.user_id
order by r.created_at desc limit 5;"
```

---

## Which model is answering

`LLM_PROVIDER=stub` replies with `[STUB PROVIDER — no model was called]`.
Everything else — pairing, routing, tool calls, storage, audit — is real; only
the model is faked. It exists because the Anthropic key has no credits.

Once it does:

```bash
./scripts/orchestrator.sh          # no LLM_PROVIDER, defaults to anthropic
```

Then `agent_runs.cost_usd` starts filling in, which is spec §52's cost per user.

---

## The OpenClaw path is superseded

`npm run gateway`, `scripts/install-config.sh`, `scripts/install-agent-workspace.sh`
and `openclaw/openclaw.json5` are the original runtime. It still works, but it
cannot tell users apart — its tool server has one client, so per-user scoping
would depend on an argument the model supplies.

`agent-workspace/*.md` is still live: the orchestrator reads those same files to
build its system prompt. Editing them affects both. Everything else in that
path is legacy.
