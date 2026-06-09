#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install.sh — One-shot setup for HFP MCP server on Raspberry Pi OS Bookworm
#              (or any Debian/Ubuntu system with BlueZ 5)
#
# Call audio is bridged directly over a Bluetooth SCO socket — no PipeWire /
# PulseAudio / WirePlumber configuration is required.
#
# Run as root (or with sudo):  sudo bash setup/install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-pi}}"   # override: SERVICE_USER=myuser sudo -E bash install.sh
if ! USER_ENTRY="$(getent passwd "$SERVICE_USER")"; then
    echo "ERROR: SERVICE_USER '$SERVICE_USER' does not exist" >&2
    exit 1
fi
USER_HOME="$(printf '%s' "$USER_ENTRY" | cut -d: -f6)"
SERVICE_UID="$(id -u "$SERVICE_USER")"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y \
    bluez \
    python3-dbus \
    python3-gi \
    python3-gi-cairo \
    gir1.2-glib-2.0 \
    libdbus-1-dev \
    libglib2.0-dev \
    libbluetooth-dev \
    python3-pip \
    python3-venv \
    libcairo2-dev

echo "==> Adding ${SERVICE_USER} to bluetooth group"
usermod -aG bluetooth "$SERVICE_USER" || true

echo "==> Installing D-Bus policy"
cp "$SCRIPT_DIR/bluetooth-policy.conf" /etc/dbus-1/system.d/hfp-mcp.conf
chmod 644 /etc/dbus-1/system.d/hfp-mcp.conf

echo "==> Configuring BlueZ"
BLUEZ_CONF=/etc/bluetooth/main.conf
# Enable auto-power and experimental features (needed for profile registration)
if grep -q '^\[Policy\]' "$BLUEZ_CONF" 2>/dev/null; then
    sed -i 's/^#AutoEnable.*/AutoEnable=true/' "$BLUEZ_CONF" || true
else
    printf '\n[Policy]\nAutoEnable=true\n' >> "$BLUEZ_CONF"
fi
if ! grep -q '^Experimental' "$BLUEZ_CONF" 2>/dev/null; then
    printf '\n[General]\nExperimental=true\n' >> "$BLUEZ_CONF"
fi

# NOTE: We deliberately do NOT configure WirePlumber's bluez5 headset-roles for
# hfp_hf. This server registers its own HFP Hands-Free profile and owns the
# RFCOMM link; letting WirePlumber also claim hfp_hf would make its backend
# compete for the same service-level connection. Call audio is handled by a
# direct SCO socket, so no WirePlumber HFP config is needed.

echo "==> Enabling services"
systemctl enable bluetooth
systemctl restart bluetooth

echo "==> Installing Python package"
# Install into a virtual environment in the repo
python3 -m venv --system-site-packages "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"

echo "==> Installing systemd user service"
USER_SYSTEMD_DIR="$USER_HOME/.config/systemd/user"
USER_ENV_FILE="$USER_HOME/.config/hfp-mcp.env"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$USER_SYSTEMD_DIR"
REPO_DIR_ESCAPED="$(printf '%s' "$REPO_DIR" | sed 's/[#&]/\\&/g')"
sed "s#__REPO_DIR__#$REPO_DIR_ESCAPED#g" "$SCRIPT_DIR/hfp-mcp.service" > "$USER_SYSTEMD_DIR/hfp-mcp.service"
chown "$SERVICE_USER:$SERVICE_GROUP" "$USER_SYSTEMD_DIR/hfp-mcp.service"
chmod 644 "$USER_SYSTEMD_DIR/hfp-mcp.service"

if [ ! -f "$USER_ENV_FILE" ]; then
    install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SCRIPT_DIR/hfp-mcp.env.example" "$USER_ENV_FILE"
fi

loginctl enable-linger "$SERVICE_USER"
systemctl start "user@$SERVICE_UID.service"
for _ in 1 2 3 4 5; do
    [ -S "/run/user/$SERVICE_UID/bus" ] && break
    sleep 1
done
runuser -u "$SERVICE_USER" -- env XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" systemctl --user daemon-reload
runuser -u "$SERVICE_USER" -- env XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" systemctl --user enable --now hfp-mcp.service

echo "==> Installing Hermes call-awareness plugin"
HERMES_PLUGIN_DIR="$USER_HOME/.hermes/plugins/hfp-call-awareness"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$HERMES_PLUGIN_DIR"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_plugin/plugin.yaml" "$HERMES_PLUGIN_DIR/"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_plugin/__init__.py" "$HERMES_PLUGIN_DIR/"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_plugin/hooks.py" "$HERMES_PLUGIN_DIR/"
echo "    → Installed to $HERMES_PLUGIN_DIR"
echo "    → Restart Hermes to activate (or add to config.yaml: plugins: [hfp-call-awareness])"

echo "==> Installing Hermes HFP phone platform plugin"
HERMES_PHONE_PLUGIN_DIR="$USER_HOME/.hermes/plugins/hfp-phone"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$HERMES_PHONE_PLUGIN_DIR"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_platforms/hfp_phone/plugin.yaml" "$HERMES_PHONE_PLUGIN_DIR/"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_platforms/hfp_phone/__init__.py" "$HERMES_PHONE_PLUGIN_DIR/"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$REPO_DIR/hermes_platforms/hfp_phone/adapter.py" "$HERMES_PHONE_PLUGIN_DIR/"
echo "    → Installed to $HERMES_PHONE_PLUGIN_DIR"
echo "    → Enable with: hermes plugins enable hfp-phone"

echo "==> Making Pi discoverable (pair your phone now if not already done)"
bluetoothctl power on   || true
bluetoothctl pairable on || true
bluetoothctl discoverable on || true

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Installation complete!                                  ║"
echo "║                                                          ║"
echo "║  1. Pair your Android phone via Bluetooth settings now   ║"
echo "║     (Pi is discoverable for 3 minutes)                   ║"
echo "║                                                          ║"
echo "║  2. Check the MCP server:                                ║"
echo "║     systemctl --user status hfp-mcp                      ║"
echo "║                                                          ║"
echo "║  3. Add to your Hermes config.yaml (see README.md):      ║"
echo "║     mcp_servers: [hfp-mcp]                               ║"
echo "║     plugins: [hfp-call-awareness]                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
