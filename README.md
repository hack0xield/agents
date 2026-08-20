# Trading Assistant — Agents

A Telegram AI trading assistant, built to the spec in [spec.txt](spec.txt).
Multi-user, grounded in real backtests and a live MT5 account, and constrained
so it will not invent statistics.

Plan and status: [POC_PLAN.md](POC_PLAN.md).

## Shape

```text
   Telegram
      │ long polling
      ▼
 apps/orchestrator      identity, sessions, agent loop, audit trail
      │           └──►  Anthropic API  (or Ollama, or a stub)
      ▼ MCP :8081
 mcp_server             12 read-only tools + backtest execution
      │           └──►  ../trading/runs   backtests, read live
      ▼ HTTP :8082
 mt5_bridge             the only thing that talks to MT5 (runs under Wine)
      ▼
   MT5 terminal ──► broker

 postgres :5432         users, accounts, conversations, agent_runs
```

## Setup

```bash
cp .env.example .env         # fill in the bot token and API key
uv venv .venv && uv pip install --python .venv/bin/python \
    anthropic sqlalchemy "psycopg[binary]" mcp
cp mt5_bridge/accounts.example.json mt5_bridge/accounts.json   # MT5 credentials
```

## Run

Bottom-up — each layer needs the one beneath it.

```bash
docker compose up -d          # 1. postgres
./scripts/mt5-bridge.sh       # 2. MT5 bridge      (terminal 1)
./scripts/mcp-server.sh       # 3. tools           (terminal 2)
./scripts/orchestrator.sh     # 4. the app         (terminal 3)
```

Without the bridge, `mt5.*` reports `DISCONNECTED` and everything else works.

## Connect a user

Starting the processes does not create anyone.

```bash
./scripts/pair.py "Name"                       # prints a t.me deep link
./scripts/connect-account.py --user <uuid> \
    --login 110119104 --server MetaQuotes-Demo \
    --nickname "Demo" --ref founder-demo       # optional
```

Open the link in Telegram and press Start. Identity comes from the Telegram
sender id — never from message content, and never from the model.

## Model providers

`LLM_PROVIDER` in `.env` picks one; each keeps its own model setting, so
switching is a single line.

| | Model | Cost | Notes |
|---|---|---|---|
| `anthropic` | `claude-sonnet-5` | ~$0.03–0.07/turn | prompt caching, per-token cost tracking |
| `ollama` | `gpt-oss:120b-cloud` | free tier | ~6s/turn, no caching. `./scripts/ollama.sh` |
| `stub` | none | free | no model at all; tests the plumbing |

## Tools

```bash
./scripts/list-tools.sh
```

`mt5.get_account` · `get_positions` · `get_trade_history` ·
`get_connection_status` · `backtests.search` · `get_summary` · `get_report` ·
`get_series` · `send_report` · `list_strategies` · `run` · `run_zone_study`

All read-only except `send_report` and the two run tools, which are annotated
honestly. **There is no tool that can place, modify or close a trade** (§10).

`backtests.run` is a temporary deviation from §33 —
see [docs/BACKTEST_EXECUTION.md](docs/BACKTEST_EXECUTION.md).

## Testing

```bash
./tests/smoke-mcp.sh        # every tool answers            (free, ~2s)
./tests/test-offline.sh     # mt5.* degrades to DISCONNECTED (free)
./tests/test-isolation.sh   # spec §48, no cross-user leak   (free)
./tests/run-evals.sh        # behavioural probes             (costs tokens)
```

The first three are free — run them constantly. `run-evals.sh` needs a model
and does not auto-grade; score it against
[tests/agent-evals/behaviour.md](tests/agent-evals/behaviour.md).

## Where things are

```text
apps/orchestrator/     the app: identity, agent loop, providers, commands
mcp_server/            tools; backtests read ../trading/runs live
mt5_bridge/            read-only MT5 facade, runs under Wine
agent-workspace/       the agent's behaviour — SOUL.md is the important one
scripts/               run and setup
tests/                 verification, plus the eval set
docs/                  MT5, backtest execution, orchestrator design, usage
```

## Design notes

- [docs/MT5.md](docs/MT5.md) — per-user accounts, capacity measurements, what is still untested
- [docs/BACKTEST_EXECUTION.md](docs/BACKTEST_EXECUTION.md) — the §33 deviation and how to revert it
- [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) — why the OpenClaw runtime was replaced
- [docs/USAGE.md](docs/USAGE.md) — what to ask, what is locked down, what is not
