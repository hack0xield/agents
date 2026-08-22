#!/usr/bin/env bash
# Install the stack as systemd *user* units, for this user and this checkout.
#
#   ./deploy/install.sh              workstation: the three app services
#   ./deploy/install.sh --headless   server: also Xvfb and a warm MT5 terminal
#
# User units rather than system units because the two environments differ in
# both the account and the path — `User=trading` and an absolute
# /home/trading/... is why a copy of the system units failed on a workstation
# with status=217/USER before it ever ran a command. A user unit runs as
# whoever installs it, and the repo path is substituted in here.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
CONF_DIR="$HOME/.config/trading-assistant"
APP_UNITS=(trading-assistant.target mt5-bridge.service mcp-server.service orchestrator.service)
MT5_UNITS=(xvfb.service mt5-terminal.service)

HEADLESS=0
[ "${1:-}" = "--headless" ] && HEADLESS=1

units=("${APP_UNITS[@]}")
if [ "$HEADLESS" = 1 ]; then
    units+=("${MT5_UNITS[@]}")
    display=":99"                       # served by xvfb.service
else
    # A workstation already has an X server; borrow the session's display so
    # Wine draws somewhere real. Falls back to :0, the common case.
    display="${DISPLAY:-:0}"
fi

mkdir -p "$UNIT_DIR" "$CONF_DIR"

# Paths the units cannot express: WINEPREFIX differs per user, DISPLAY differs
# per host. Kept out of the units so changing them needs no reinstall.
cat > "$CONF_DIR/env" <<EOF
# Written by deploy/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Edit freely;
# systemctl --user restart trading-assistant.target picks changes up.
DISPLAY=$display
WINEPREFIX=$HOME/.mt5
WINEDEBUG=-all
WINEDLLOVERRIDES=mscoree,mshtml=
EOF

for u in "${units[@]}"; do
    sed "s|@REPO@|$REPO|g" "$REPO/deploy/units/$u" > "$UNIT_DIR/$u"
done

# Without linger, user units die at logout — which is the entire problem this
# is meant to solve. Not fatal if it fails: on a desktop you are logged in.
if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -qx yes; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || sudo loginctl enable-linger "$USER" 2>/dev/null \
        || echo "! could not enable linger — run: sudo loginctl enable-linger $USER"
fi

systemctl --user daemon-reload

echo "installed ${#units[@]} unit(s) for $USER"
echo "  repo    $REPO"
echo "  display $display"
echo "  units   $UNIT_DIR"
[ "$HEADLESS" = 1 ] && echo "  headless: xvfb + mt5-terminal included"
echo
echo "next:"
[ "$HEADLESS" = 1 ] && echo "  systemctl --user enable --now xvfb mt5-terminal"
echo "  systemctl --user enable --now trading-assistant.target"
