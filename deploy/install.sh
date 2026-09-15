#!/usr/bin/env bash
# Install the stack as systemd *user* units, for this user and this checkout.
#
#   ./deploy/install.sh              workstation: the app services
#   ./deploy/install.sh --headless   server: also Xvfb, a warm MT5 terminal and
#                                    the live-trader@ template
#
# User units rather than system units because the two environments differ in
# both the account and the path — `User=trading` and an absolute
# /home/trading/... is why a copy of the system units failed on a workstation
# with status=217/USER before it ever ran a command. A user unit runs as
# whoever installs it, and the repo path is substituted in here.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The backtester checkout sits beside this one, as the MCP server expects.
TRADING="$(cd "$REPO/../trading" 2>/dev/null && pwd || echo "$REPO/../trading")"
UNIT_DIR="$HOME/.config/systemd/user"
CONF_DIR="$HOME/.config/trading-assistant"
APP_UNITS=(trading-assistant.target mt5-bridge.service mcp-server.service
           orchestrator.service reports-server.service live-notify.service)
# live-trader@ trades through this host's terminal, so it goes where the
# terminal does. A workstation copy would trade the same shared account as the
# server's, and each would close the other's positions as leftovers.
MT5_UNITS=(xvfb.service mt5-terminal.service live-trader@.service)

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

# The reports view is the one thing meant to be reachable from outside, and
# only on a server. A workstation keeps it on loopback: there is nothing to
# reach it from, and binding wider on a laptop on someone's wifi is not a
# default worth having.
if [ "$HEADLESS" = 1 ]; then
    reports_bind="0.0.0.0"
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || true)"
    reports_url="http://${ip:-127.0.0.1}:8083"
else
    reports_bind="127.0.0.1"
    reports_url="http://127.0.0.1:8083"
fi

# live/ holds one env file per live-trader instance, written by live.start.
mkdir -p "$UNIT_DIR" "$CONF_DIR" "$CONF_DIR/live"

# Paths the units cannot express: WINEPREFIX differs per user, DISPLAY differs
# per host. Kept out of the units so changing them needs no reinstall.
cat > "$CONF_DIR/env" <<EOF
# Written by deploy/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Edit freely;
# systemctl --user restart trading-assistant.target picks changes up.
DISPLAY=$display
WINEPREFIX=$HOME/.mt5
WINEDEBUG=-all
WINEDLLOVERRIDES=mscoree,mshtml=
REPORTS_BIND=$reports_bind
REPORTS_PORT=8083
PUBLIC_REPORTS_URL=$reports_url
EOF

for u in "${units[@]}"; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@TRADING@|$TRADING|g" "$REPO/deploy/units/$u" > "$UNIT_DIR/$u"
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
echo "  trading $TRADING"
echo "  display $display"
echo "  reports $reports_url (bind $reports_bind)"
echo "  units   $UNIT_DIR"
[ "$HEADLESS" = 1 ] && echo "  headless: xvfb, mt5-terminal and live-trader@ included"
echo
echo "next:"
[ "$HEADLESS" = 1 ] && echo "  systemctl --user enable --now xvfb mt5-terminal"
echo "  systemctl --user enable --now trading-assistant.target"
