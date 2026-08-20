# Working in this repo

## Stop what you start

Every process you start, you stop before finishing your turn. If it was started
to check something, kill it once you have the answer — do not leave it running
because the user might want it.

| Service | Port |
|---|---|
| MT5 bridge | 8082 |
| MCP server | 8081 |
| OpenClaw gateway (legacy) | 18789 |
| Postgres (docker) | 5432 — usually left running |

`fuser -k 8082/tcp` is the reliable stop. **Never `pkill -f` / `pgrep -f` with a
pattern that appears in your own command line** — it matches the shell running
it and kills your session.

If a terminal is wedged, find it by TTY, not by name. Wine's stub is
`start.exe /exec …` and matches no search for "bridge" or "terminal64":

```bash
ps -eo pid,tty,stat,args | awk '$2 ~ /pts/ && $3 ~ /\+/'
```

The MT5 terminal (`terminal64.exe`) is shared and slow to start — the bridge
leaves it running deliberately. More than one of it is a bug.
