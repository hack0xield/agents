# Trading Assistant — Agents

POC of the Telegram AI trading assistant described in [spec.txt](spec.txt).
Build plan: [POC_PLAN.md](POC_PLAN.md).

**Test mode.** No payments, no real MT5, no backtest engine. Phase 1 is a
Telegram bot backed by Claude with the product's tool lockdown already in place.

## Prerequisites

Node ≥22.22.3 is required by OpenClaw. The system node here is v20.9.0, so a
private copy lives in `~/.local/n` and is prepended to `PATH` only for the
processes this repo starts (see `scripts/node-env.sh`). The global node is
untouched.

To reinstall it:

```bash
N_PREFIX="$HOME/.local/n" n 24
```

## Setup

```bash
npm install                  # installs openclaw locally, no sudo, no global state
cp .env.example .env         # then fill it in — see below
./scripts/install-config.sh  # openclaw/openclaw.json5 -> ~/.openclaw/openclaw.json
```

`.env` needs:

| Variable | Where from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_BOT_USERNAME` | the bot's username, no `@` (Phase 2) |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys |

## Run

```bash
npm run gateway              # loads .env, starts the OpenClaw gateway
```

Then DM the bot on Telegram. Because `dmPolicy: "pairing"`, the first message
from an unknown account is held pending approval:

```bash
./scripts/oc pairing list telegram
./scripts/oc pairing approve telegram <CODE>
```

Codes expire after 1 hour. Phase 2 automates this side against our own pairing
tokens.

## Layout

```text
openclaw/openclaw.json5   versioned gateway config (no secrets)
scripts/node-env.sh       puts Node 24 on PATH
scripts/install-config.sh installs the config to ~/.openclaw/
scripts/gateway.sh        loads .env, starts the gateway
scripts/oc                openclaw CLI wrapper with .env + Node 24 loaded
```

## Config notes

Verified against OpenClaw **2026.7.1-2** with `openclaw config schema`:

- Agents live under `agents.list` (an **array**), not `agents.entries`.
- Tool profile is `messaging`, not `minimal`, because MCP tools are only
  surfaced in the coding/messaging profiles — Phase 4 needs that.
- `exec`, `process`, `write`, `edit`, `apply_patch`, `browser`,
  `sessions_spawn` and `subagents` are denied per spec.txt §49. This is set
  before the bot is reachable, not after.
- `memorySearch` is off: it defaults to an OpenAI embedding provider we have no
  key for and do not want as a dependency.
- `gateway.mode` must be set. Without it the gateway refuses to start, treating
  the config as possibly clobbered.
- `agents.defaults.model` is pinned as well as the per-agent model. Without the
  defaults-level pin the runtime resolves to `openai/gpt-5.5`.

### Config ownership

`~/.openclaw/openclaw.json` is **not** a copy of the versioned file. OpenClaw
writes to it at runtime — approving a Telegram pairing adds
`commands.ownerAllowFrom`, for instance — and it stores plain JSON, so comments
are stripped.

`install-config.sh` therefore uses `openclaw config patch`, which merges, rather
than `cp`, which would wipe that state. Our file owns the keys it declares;
everything else is left alone. Edit `openclaw/openclaw.json5` and re-run the
script; never hand-edit the live file.

### Gateway auth

The gateway binds to `127.0.0.1:18789`. `gateway.auth.token` protects
non-loopback clients only — OpenClaw auto-approves device pairing for loopback
connects, so any process running as this user can drive the gateway. This
machine is the trust boundary until `bind` stops being `loopback`.
