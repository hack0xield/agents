# Deploying to a fresh Vultr instance

Reproducible build of the host that runs the whole stack: Postgres, the MT5
bridge under Wine, the MCP server, the orchestrator, and the `trading`
backtesting repo.

Verified 2026-08-21 against a fresh Vultr instance, **Ubuntu 26.04 LTS
(`resolute`)**, 2 vCPU / 4 GB / 128 GB NVMe.

## Instance sizing

Measured footprints, not estimates — sources in [MT5.md](MT5.md) and
`../experiments/RESULTS.md`.

| | RAM | Disk |
|---|---|---|
| Base OS + Docker + Postgres + Xvfb + wineserver | ~1.0 GB | — |
| `terminal64.exe` — **per connected account** | 249–257 MB (measured here) | 298 MB |
| Backtest subprocess (transient, ~1.7 s) | 201 MB | 4.5 MB per run |
| Wine prefix, first MT5 install | — | 4.7 GB |
| Both repos + venvs | — | ~1.2 GB |

A 4 GB instance reports **3.3 GB usable**. That comfortably holds 1–3
connected MT5 accounts; RAM binds at roughly 4–5. Disk is not the constraint —
after full base setup plus both repos the box sits at 16 GB of 120 GB.

Measured on the instance, and **faster than the 12-core workstation** —
NVMe and a higher clock beat core count for this workload:

| | Workstation | Instance |
|---|---|---|
| `day_open` backtest | 3.29 s / 274 MB | **1.66 s / 201 MB** |

It pins a single core at ~99%. The 600 s backtest timeout in
`mcp_server/server.py` is a guard rail with roughly 360× headroom, not an
expected runtime.

Watch `runs-adhoc/`: 4.5 MB per backtest, agent-initiated with no quota. Set a
retention policy before running unattended.

## 0. Prerequisites on the workstation

An ed25519 key registered with Vultr at instance-creation time:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/id_ed25519_vultr_trading \
    -C "vultr-trading-assistant"
ssh-keygen -p -f ~/.ssh/id_ed25519_vultr_trading   # set a passphrase
```

`~/.ssh/config` — `IdentitiesOnly` stops other keys being offered to the host,
and `SetEnv` prevents the client forwarding locales the server lacks:

```sshconfig
Host vultr-trading            # root, for provisioning only
    HostName <instance-ip>
    User root
    IdentityFile ~/.ssh/id_ed25519_vultr_trading
    IdentitiesOnly yes
    SetEnv LC_ALL=C.UTF-8

Host vultr-trading-app        # the app user; Wine refuses to run as root
    HostName <instance-ip>
    User trading
    IdentityFile ~/.ssh/id_ed25519_vultr_trading
    IdentitiesOnly yes
    SetEnv LC_ALL=C.UTF-8
```

Verify before continuing: `ssh vultr-trading 'hostname'`.

## 1. Application user

Wine will not run as root, so the app user is a hard requirement, not hygiene.

```bash
ssh vultr-trading '
adduser --disabled-password --gecos "" trading
usermod -aG sudo trading
echo "trading ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-trading
chmod 440 /etc/sudoers.d/90-trading
install -d -m 700 -o trading -g trading /home/trading/.ssh
cp /root/.ssh/authorized_keys /home/trading/.ssh/authorized_keys
chown trading:trading /home/trading/.ssh/authorized_keys
chmod 600 /home/trading/.ssh/authorized_keys'
```

## 2. Base system

`needrestart` must be silenced first or `apt upgrade` opens interactive
dialogs over SSH and hangs.

```bash
ssh vultr-trading '
export DEBIAN_FRONTEND=noninteractive
mkdir -p /etc/needrestart/conf.d
printf "%s\n" "\$nrconf{restart} = \"a\";" > /etc/needrestart/conf.d/90-noninteractive.conf
apt-get update -qq
apt-get upgrade -y -qq -o Dpkg::Options::=--force-confold
apt-get install -y -qq git curl wget ca-certificates gnupg lsb-release \
    build-essential pkg-config unzip p7zip-full jq htop tmux ufw fail2ban \
    python3 python3-venv python3-pip python3-dev'
```

## 3. Python — pin 3.13, do not use the system interpreter

Ubuntu 26.04 ships **Python 3.14**. The repos are built against 3.13, and
pyarrow / psycopg wheels for 3.14 are not dependable yet. `uv` fetches a
standalone 3.13 and sidesteps the question.

```bash
ssh vultr-trading-app '
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.13'
```

## 4. Docker

Docker's official repo carries `resolute`. Ubuntu's `docker.io` +
`docker-compose-v2` also work if it ever lags a release.

```bash
ssh vultr-trading '
export DEBIAN_FRONTEND=noninteractive
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
usermod -aG docker trading
systemctl enable --now docker'
```

## 5. Hardening

Confirm key auth works for **both** users before disabling passwords, and
allow port 22 **before** enabling ufw.

```bash
ssh vultr-trading '
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<EOF
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sshd -t && systemctl reload ssh
ufw allow 22/tcp
ufw --force enable
systemctl enable --now fail2ban'
```

No other inbound port is ever needed: Postgres (5432), the MT5 bridge (8082)
and the MCP server (8081) all bind loopback, and Telegram is outbound long
polling.

## 6. Wine and the headless display

**`mt5linux.sh` cannot be used verbatim on 26.04** — its codename logic stops
at 25.10 (`questing`) and it would select the wrong WineHQ repo. Do it
directly:

```bash
ssh vultr-trading '
export DEBIAN_FRONTEND=noninteractive
dpkg --add-architecture i386
install -dm755 /etc/apt/keyrings
wget -qO - https://dl.winehq.org/wine-builds/winehq.key \
    | gpg --dearmor -o /etc/apt/keyrings/winehq-archive.key
. /etc/os-release
wget -qNP /etc/apt/sources.list.d/ \
    "https://dl.winehq.org/wine-builds/ubuntu/dists/$VERSION_CODENAME/winehq-$VERSION_CODENAME.sources"
apt-get update -qq
apt-get install -y -qq --install-recommends winehq-staging
apt-get install -y -qq xvfb x11-utils'
```

`terminal64.exe` is a GUI application and a VPS has no X server, so it needs a
virtual framebuffer permanently available. **Nothing in either repo sets this
up** — it is the first thing that fails on a fresh host.

```bash
ssh vultr-trading '
cat > /etc/systemd/system/xvfb.service <<EOF
[Unit]
Description=Virtual framebuffer X server (headless display for MT5 under Wine)
After=network.target

[Service]
User=trading
Group=trading
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now xvfb'

ssh vultr-trading-app '
printf "\nexport DISPLAY=:99\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n" >> ~/.profile'
```

Verify: `ssh vultr-trading-app 'DISPLAY=:99 xdpyinfo | head -1'`.

## 7. Repository access — deploy keys, generated on the server

Do **not** copy a personal GitHub key to the host. Generate one key per repo
on the server itself, so the private half never travels, and register each as
a **read-only deploy key** — a compromised host can then pull but never push.

GitHub allows a given deploy key on only one repository, hence two keys and
two host aliases.

```bash
ssh vultr-trading-app '
for repo in trading agents; do
  ssh-keygen -t ed25519 -a 100 -f ~/.ssh/id_ed25519_deploy_$repo -N "" \
      -C "vultr-deploy-$repo"
done
cat > ~/.ssh/config <<EOF
Host github-trading
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_deploy_trading
    IdentitiesOnly yes

Host github-agents
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_deploy_agents
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
cat ~/.ssh/id_ed25519_deploy_trading.pub ~/.ssh/id_ed25519_deploy_agents.pub'
```

Add each printed key at
`github.com/hack0xield/<repo>/settings/keys/new`, **leaving "Allow write
access" unchecked**.

Verify: `ssh vultr-trading-app 'ssh -T git@github-trading'` should greet you
by repo name rather than refusing with `Permission denied (publickey)`.

## 8. Clone

The host aliases replace `github-as-anon` in the clone URL; everything else is
unchanged. Keep the two repos siblings — `mcp_server` reaches the backtests by
the relative path `../trading/runs`.

```bash
ssh vultr-trading-app '
mkdir -p ~/trading_assistant && cd ~/trading_assistant
git clone git@github-trading:hack0xield/trading.git
git clone git@github-agents:hack0xield/agents.git'
```

## 9. Python environments — **both repos**

Two separate venvs. Missing the second one fails late and unhelpfully:
`mcp-server.sh` and `orchestrator.sh` both die with
`.venv/bin/python: No such file or directory`.

```bash
ssh vultr-trading-app '
export PATH="$HOME/.local/bin:$PATH"

cd ~/trading_assistant/trading
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest tests/ -q

cd ~/trading_assistant/agents
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python anthropic sqlalchemy "psycopg[binary]" mcp'
```

Expect **257 passed, 1 skipped** in about 1 s from the `trading` suite.
(`trading/CLAUDE.md` still says 122 — it is stale.) It needs no market data,
so it passes before step 10.

## 10. The data git does not carry

`trading/.gitignore` excludes `/data/` and `/runs/` as "regenerable, and
large". Only half of that is true, and the difference matters:

- **`data/bars`** really is regenerable — re-fetched from MT5 via
  `fetch-mt5.sh`, which needs a working terminal under Wine. Until step 11
  exists on a fresh host, there is nothing to regenerate *from*, so seed it.
- **`runs/`** is **not** regenerable. It is the curated, human-reviewed
  backtest evidence that `backtests.search` / `get_summary` / `get_report` /
  `get_series` read live, and that spec §9.1 treats as Level A evidence.
  Re-running the backtester produces new artifacts, not the reviewed ones.
  **A fresh host without it gives the assistant no evidence base at all.**

There is currently no distribution mechanism for it. Until there is, seed both
directories from the workstation:

```bash
cd <local>/trading_assistant/trading
rsync -az data/ vultr-trading-app:~/trading_assistant/trading/data/
rsync -az runs/ vultr-trading-app:~/trading_assistant/trading/runs/
```

`runs-adhoc/` is deliberately not copied — it is exploratory output, and
[BACKTEST_EXECUTION.md](BACKTEST_EXECUTION.md) explains why it stays separate
from the reviewed set.

### Verify the backtester end to end

```bash
ssh vultr-trading-app 'cd ~/trading_assistant/trading &&
  .venv/bin/python scripts/run_backtest.py --strategy day_open --symbol XAUUSD \
    --timeframe M15 --intrabar conservative --save --runs-dir runs-adhoc \
    --label probe --json --quiet'
```

The trade count must match what the same config produces on the workstation —
252 for `day_open XAUUSD M15`. A different number means the bars differ, not
that the engine does.

## 11. MT5 under Wine

Nothing in either repo builds the prefix — `fetch-mt5.sh` and `bridge.py` both
assume `C:\Python311\python.exe` and a terminal already exist. Build it as
**`trading`, never as root**: as root, `$HOME` is `/root` so `WINEPREFIX`
resolves to `/root/.mt5`, and Wine will not run as root anyway.

```bash
ssh vultr-trading-app '
export WINEPREFIX="$HOME/.mt5" WINEARCH=win64 DISPLAY=:99 WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="   # MT5 needs neither mono nor gecko
wineboot --init && wineserver -w

mkdir -p ~/mt5-install && cd ~/mt5-install
wget -q https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
wine python-3.11.9-amd64.exe /quiet InstallAllUsers=1 TargetDir=C:\\Python311 \
    PrependPath=1 Include_test=0 Include_launcher=0 Include_doc=0
wineserver -w
wine "C:\\Python311\\python.exe" -m pip install MetaTrader5==5.0.5735

wget -q https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
wine mt5setup.exe /auto
wineserver -w'
```

Pin `MetaTrader5` to the workstation's version (5.0.5735) so the bridge is not
debugging a version skew on top of everything else.

`libEGL ... DRI3` warnings under Xvfb are expected — no GPU, software
rendering — and harmless.

### The terminal must be kept warm — this is not an optimisation

`mt5.initialize()` behaves in two completely different ways:

| | Result |
|---|---|
| Terminal already running → client **attaches** | **5–7 ms**, reliable |
| Terminal not running → client **launches** it | **hangs past a 240 s timeout** |

In the second case the terminal itself starts perfectly — the journal shows it
authorized and synchronised 12,524 symbols in 1.64 s — but the Python client
never completes its handshake with the instance it spawned. So the terminal is
run as a **service**, and the client only ever attaches to a warm one.

This is not a workaround bolted on: [MT5.md](MT5.md) already establishes that
the terminal survives `mt5.shutdown()` and that terminal lifetime and
connection lifetime are independent. On a headless host that stops being a
nice property and becomes the required design.

```bash
ssh vultr-trading '
cat > /etc/systemd/system/mt5-terminal.service <<EOF
[Unit]
Description=MetaTrader 5 terminal under Wine (kept warm for the MT5 bridge)
After=network-online.target xvfb.service
Requires=xvfb.service

[Service]
User=trading
Group=trading
Environment=WINEPREFIX=/home/trading/.mt5
Environment=DISPLAY=:99
Environment=WINEDEBUG=-all
Environment=WINEDLLOVERRIDES=mscoree,mshtml=
WorkingDirectory=/home/trading
ExecStart=/usr/bin/wine "C:\\\\Program Files\\\\MetaTrader 5\\\\terminal64.exe"
ExecStopPost=/usr/bin/wineserver -k
Restart=always
RestartSec=15
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now mt5-terminal'
```

Health check — the IPC listener is the thing that matters, not the process:

```bash
ssh vultr-trading 'ss -ltn | grep 22346'
```

### First launch does two one-time things

Budget for them; neither recurs.

1. **LiveUpdate.** The terminal downloads and applies a newer build, then
   restarts. Any client waiting on IPC across that restart times out. Let it
   finish before trusting a measurement.
2. **First login** pulls the account and symbol set — **21.3 s** once, against
   1.6 s for every later start.

### Measured on this host

| | Workstation | Instance |
|---|---|---|
| Terminal start → synchronised | 3.0–3.5 s | **1.64 s** |
| Warm attach (`initialize`) | 4–7 ms | **5–7 ms** |
| `positions_get()` | <1 ms | **<1 ms** |
| `terminal64.exe` RSS | 262–300 MB | **249–257 MB** |
| First-ever login | not recorded | **21.3 s**, once |

Both cold start and footprint are better than the workstation. With the
terminal up, the whole box sits at **907 MB** of 3.3 GB.

### Verify

```bash
ssh vultr-trading-app '
export WINEPREFIX="$HOME/.mt5" DISPLAY=:99 WINEDEBUG=-all
cd ~/trading_assistant/trading
wine "C:\\Python311\\python.exe" -c "
import json, MetaTrader5 as mt5
c = json.load(open(r\"Z:\\home\\trading\\trading_assistant\\trading\\mt5-mcp-server\\config.json\"))
print(mt5.initialize(path=c[\"mt5_path\"], login=int(c[\"login\"]),
                     password=c[\"password\"], server=c[\"server\"]))
print(mt5.account_info())"'
```

## 12. Secrets — never in git, never in the image

These are gitignored by design and must be transferred out of band:

| File | Holds |
|---|---|
| `agents/.env` | Telegram bot token, Anthropic API key, `DATABASE_URL` |
| `agents/mt5_bridge/accounts.json` | MT5 credentials, mode 600 |
| `trading/mt5-mcp-server/config.json` | MT5 credential the backtester reuses |

```bash
scp agents/.env vultr-trading-app:~/trading_assistant/agents/.env
ssh vultr-trading-app 'chmod 600 ~/trading_assistant/agents/.env'
```

`notes.txt` must **not** be copied — it is a plaintext dump of the same
secrets and has no role on the server.

## 13. Run it under systemd

Three services in login shells die with the SSH connection and come back from
nothing after a reboot. The units live in [`deploy/`](../deploy/) — see
[deploy/README.md](../deploy/README.md) for why they are system units rather
than user units.

```bash
ssh vultr-trading-app 'cd ~/trading_assistant/agents && git pull'
ssh vultr-trading '
cd /home/trading/trading_assistant/agents
cp deploy/*.service deploy/*.target /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xvfb mt5-terminal
systemctl enable --now trading-assistant.target mt5-bridge mcp-server orchestrator'
```

Stop anything already running by hand first, or the services fail on a port
that is still held.

### Operating it

```bash
sudo systemctl restart trading-assistant.target   # all three app services, ~21 s
sudo systemctl restart orchestrator               # one of them
systemctl status mt5-bridge mcp-server orchestrator
journalctl -u orchestrator -f
```

The target covers the **application** layer only. `xvfb` and `mt5-terminal`
are deliberately outside it: restarting the terminal costs a login and a
12,524-symbol sync, and bouncing the app should not pay for that. A verified
`restart trading-assistant.target` left the terminal untouched — it had been
up for over an hour and stayed up.

### Verify

```bash
curl -s http://127.0.0.1:8082/health                    # bridge
curl -s "http://127.0.0.1:8082/account?ref=<your-ref>"  # CONNECTED + balance
ss -ltn | grep 8081                                     # mcp
journalctl -u orchestrator -n 5 --no-pager              # "polling telegram…"
systemctl show orchestrator -p Slice --value            # system.slice
```

`system.slice` is the part that answers "does it survive logout" — these are
system units with no session dependency, so SSH disconnects are irrelevant to
them and `loginctl enable-linger` is not needed.

### Postgres

`docker-compose.yml` carries `restart: unless-stopped`. A container created
before that line was added keeps the old policy — `docker compose up -d`
recreates it, and the named volume means the data survives:

```bash
docker inspect trading_assistant_db --format '{{.HostConfig.RestartPolicy.Name}}'
```

## Open items

Steps 1–13 are verified on this host: a live MT5 account, all four free test
suites green, and the whole stack under systemd. Still to do:

- **`runs/` has no distribution mechanism** — see step 10. It is seeded by
  rsync from one workstation, which is not a durable answer for the artifact
  the assistant's evidence hierarchy rests on.
- **`trading_accounts.connection_state` has no writer.** It defaults to
  `DISCONNECTED` and nothing updates it, so the column misleads anyone reading
  the table directly. It no longer reaches the model — the prompt stopped
  injecting it — but the snapshot poller ([MT5.md](MT5.md) §5 step 2) is what
  would make the field honest.

**Closed since first deployment:** the account now reports
`access: investor_read_only` and `trade_allowed: false`, so the master-password
finding in [MT5.md](MT5.md) §1 no longer applies to this host.
