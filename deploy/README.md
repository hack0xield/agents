# systemd units

System units rather than user units (`~/.config/systemd/user`), because
`xvfb` and `mt5-terminal` must be ordered before the app services and a user
unit cannot reliably `After=` a system one. The cost is `sudo` on every
command; the benefit is that ordering actually holds, and nothing depends on
`loginctl enable-linger`.

Paths are absolute for the deployed layout — `trading` user, repo at
`~/trading_assistant/agents`. `%h` only expands in user units.

## Install

```bash
sudo cp deploy/*.service deploy/*.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb mt5-terminal
sudo systemctl enable --now trading-assistant.target
sudo systemctl enable --now mt5-bridge mcp-server orchestrator
```

## Operate

The whole application layer, as one:

```bash
sudo systemctl start   trading-assistant.target   # ~20 s to serving
sudo systemctl stop    trading-assistant.target
sudo systemctl restart trading-assistant.target   # ~21 s
```

`stop` and `restart` reach the three services through their `PartOf=`;
`start` pulls them in through the target's `Wants=`. All three verified.

One service at a time:

```bash
sudo systemctl start   orchestrator
sudo systemctl stop    orchestrator
sudo systemctl restart orchestrator
```

Restart `mcp-server` after any change to `.env` — the script sources it once
at exec, so a running process keeps the old values while looking healthy.

Inspect:

```bash
systemctl status mt5-bridge mcp-server orchestrator
systemctl is-active mt5-bridge mcp-server orchestrator   # scriptable
journalctl -u orchestrator -f                            # follow
journalctl -u mcp-server -n 50 --no-pager                # recent
systemctl cat orchestrator                               # effective config
```

The infrastructure services are separate on purpose and take the same verbs:

```bash
sudo systemctl restart mt5-terminal   # costs a login + 12,524-symbol sync
sudo systemctl restart xvfb           # restarts the terminal with it
```

Health, without going through the model:

```bash
curl -s http://127.0.0.1:8082/health              # bridge + known refs
ss -ltn | grep -E '8081|8082|22346'               # mcp, bridge, MT5 IPC
docker ps --filter name=trading_assistant_db      # postgres
```

`trading-assistant.target` covers the three application services only.
`xvfb` and `mt5-terminal` are excluded on purpose: restarting the terminal
costs a login and a 12,524-symbol sync, and bouncing the app should not pay
for it.

## Layering

```
xvfb ─► mt5-terminal ─► mt5-bridge ─► mcp-server ─► orchestrator
                                          ▲
                        docker (postgres) ┘
```

Every arrow except `xvfb ─► mt5-terminal` is `Wants=`, not `Requires=`. Each
layer degrades honestly when the one beneath it is missing — `mt5.*` answers
`DISCONNECTED` rather than an empty list — so a restart underneath must not
cascade into stopping everything above.
