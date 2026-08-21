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

```bash
sudo systemctl restart trading-assistant.target   # all three app services
sudo systemctl restart orchestrator               # just one
systemctl status mt5-bridge mcp-server orchestrator
journalctl -u orchestrator -f                     # follow logs
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
