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
./deploy/install.sh              # workstation — the three app services
./deploy/install.sh --headless   # server — also Xvfb and a warm MT5 terminal
```

Then, as the script prints:

```bash
systemctl --user enable --now xvfb mt5-terminal        # --headless only
systemctl --user enable --now trading-assistant.target
```

### Why user units

The workstation and the server differ in **both** the account and the path —
`epershyn` at `~/Documents/trading_assistant/agents` versus `trading` at
`~/trading_assistant/agents`. System units have to name both absolutely, so
copying the server's units to a workstation fails with `status=217/USER`
before it runs a single command: `User=trading` does not exist there.

A user unit runs as whoever installs it, `%h` resolves per user, and
`install.sh` substitutes the repo path. One procedure, both hosts.

`install.sh` also enables **linger** — without it user units die at logout,
which is the whole problem this exists to solve.

### What differs between the two

Everything host-specific lives in `~/.config/trading-assistant/env`, written
by the installer and safe to edit afterwards:

| | Workstation | Server |
|---|---|---|
| `DISPLAY` | the live session (`:0`/`:1`) | `:99`, served by `xvfb` |
| `WINEPREFIX` | `~/.mt5` | `~/.mt5` |
| Xvfb, warm terminal | not installed | installed |

Changing that file needs a restart, not a reinstall.

## Two things MT5 forces on the design

**The terminal is supervised by a script, not by `wine` directly.**
`ExecStart=wine terminal64.exe` cannot work: once MT5 has applied a LiveUpdate
it re-execs itself as `terminal64.exe /skipupdate:<token> /portable` and the
launching wine exits 0 after ~2 s. `Type=simple` reads that as the service
dying and restarts forever while the terminal runs perfectly; `Type=forking`
finds no main PID, because MT5 writes no PID file and the cgroup holds
wineserver, two winedevice processes and the terminal. So
[`scripts/mt5-terminal.sh`](../scripts/mt5-terminal.sh) launches it and blocks
until it is gone, giving the unit a lifetime that matches the terminal's.

A fresh install hides this — the first launch, before any update, does stay in
the foreground.

**The bridge uses `KillMode=process`.** Its script stops the bridge and leaves
the terminal running on purpose. The default control-group kill contradicts
that: it SIGTERMs wineserver and winedevice too, they do not exit, and the
unit sits out `TimeoutStopSec` before being SIGKILLed and marked failed. Only
shows up where the terminal is not a separate unit — i.e. on a workstation.

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
