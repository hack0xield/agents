# systemd units

Runs the stack as systemd **user** units, on a workstation or the server.

## Install

```bash
./deploy/install.sh              # workstation — the three app services
./deploy/install.sh --headless   # server — also Xvfb and a warm MT5 terminal

systemctl --user enable --now xvfb mt5-terminal        # --headless only
systemctl --user enable --now trading-assistant.target
```

## Status

```bash
systemctl --user status trading-assistant.target
systemctl --user list-units 'mt5-*' 'mcp-*' 'orchestrator*' 'xvfb*'
systemctl --user is-active mt5-bridge mcp-server orchestrator
```

## Start, stop, restart

```bash
systemctl --user restart trading-assistant.target   # all three, ~22 s
systemctl --user stop    trading-assistant.target
systemctl --user start   trading-assistant.target

systemctl --user restart orchestrator               # one service
```

Restart `mcp-server` after editing `.env` — it reads the file once at exec, so
a running process holds stale values while still reporting healthy.

The target covers the three app services. `xvfb` and `mt5-terminal` are
outside it, so restarting the app does not cost a terminal login and a
12,524-symbol sync:

```bash
systemctl --user restart mt5-terminal
```

## Logs

```bash
journalctl --user -u mt5-bridge -u mcp-server -u orchestrator -f
journalctl --user -u orchestrator -f          # follow
journalctl --user -u mcp-server -n 50         # recent
journalctl --user -u mt5-bridge --since '10 min ago'
```

## Health, without going through the model

```bash
curl -s http://127.0.0.1:8082/health          # bridge + known refs
ss -ltn | grep -E '8081|8082|22346'           # mcp, bridge, MT5 IPC
docker ps --filter name=trading_assistant_db  # postgres
```

## Configuration

| | Where |
|---|---|
| Unit definitions | `deploy/units/*` — edit, then re-run `install.sh` |
| Installed copies | `~/.config/systemd/user/` — generated, do not edit |
| `DISPLAY`, `WINEPREFIX` | `~/.config/trading-assistant/env` — edit, then restart |
| Secrets | `.env`, gitignored, read by the scripts |

`install.sh` substitutes the repo path and enables linger. Re-run it after
changing anything in `deploy/units/`.

## Notes

- **User units, not system units.** The two hosts differ in both account and
  path, so `User=trading` with an absolute `/home/trading/...` fails on a
  workstation with `status=217/USER`.
- **One orchestrator at a time.** A Telegram token allows one long-poller; two
  get 409s and split updates unpredictably.
- MT5 forces two unobvious settings — `Type=simple` against a supervisor
  script, and `KillMode=process` on the bridge. Both are explained in comments
  in `deploy/units/mt5-terminal.service` and `mt5-bridge.service`.
