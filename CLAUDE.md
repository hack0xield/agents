# Working in this repo

## Leave the machine as you found it

**Every process you start, you stop before finishing your turn.** If you started
it to check something, kill it once you have the answer. Do not leave a service
running because the user might want it — say it is stopped and give them the
command.

This repo runs four long-lived services, and leaving them behind has cost real
time: a gateway that blocked `npm run gateway` with a misleading "already
running under systemd" error, two MT5 terminals at 300 MB each, 11 orphaned
Wine processes, and a `start.exe` stub that wedged the user's terminal for half
an hour.

| Service | Port | Stop with |
|---|---|---|
| Postgres (docker) | 5432 | `docker compose down` (usually leave running) |
| MT5 bridge | 8082 | `fuser -k 8082/tcp` |
| MCP server | 8081 | `fuser -k 8081/tcp` |
| OpenClaw gateway (legacy) | 18789 | `fuser -k 18789/tcp` |

The MT5 **terminal** (`terminal64.exe`) is shared and slow to start; the bridge
leaves it running on purpose. More than one of it is a bug.

## Killing things correctly

**Never `pkill -f` / `pgrep -f` with a pattern that appears in your own command
line.** The pattern matches the shell running it and kills your own session.
This has happened four times. Use:

```bash
fuser -k 8082/tcp                                  # by port — preferred
kill <pid>                                          # by explicit pid
```

**When a terminal is wedged, search by TTY, not by name.** Wine's launcher stub
is `start.exe /exec C:\Python311\python.exe` — it contains no "bridge", no
"terminal64", and survives every name-based search:

```bash
ps -eo pid,tty,stat,args | awk '$2 ~ /pts/ && $3 ~ /\+/'
```

That lists what is actually holding each terminal.

## Verifying

Prefer a check that leaves nothing behind. Where a service must run for a test,
start it, test, stop it in the same command block.

```bash
./tests/smoke-mcp.sh        # tools, needs mcp-server up
./tests/test-offline.sh     # mt5.* degrades honestly, no services needed
./tests/test-isolation.sh   # spec §48, no API key needed
./tests/run-evals.sh        # behavioural probes — costs tokens
```

`test-isolation.sh` creates users; they accumulate. Clear them when done.

## Two standing constraints

- **No credentials in Postgres** (spec §47). `trading_accounts` holds a
  `credential_ref`; only `mt5_bridge/accounts.json` resolves it to a secret.
- **No tool that can trade** (spec §10). The MT5 bridge imports three read
  functions and no order function — absence, not a flag.
