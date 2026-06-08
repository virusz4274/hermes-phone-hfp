#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install.sh — One-shot setup for HFP MCP server on Raspberry Pi OS Bookworm
#              (or any Debian/Ubuntu system with BlueZ 5 + PipeWire)
#
# Run as root (or with sudo):  sudo bash setup/install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="${SERVICE_USER:-pi}"   # override: SERVICE_USER=myuser sudo bash install.sh

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
    pipewire \
    pipewire-pulse \
    wireplumber \
    portaudio19-dev \
    python3-pip \
    python3-venv

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
if ! grep -q 'ExperimentalFeatures' "$BLUEZ_CONF" 2>/dev/null; then
    printf '\n[General]\nExperimentalFeatures=true\n' >> "$BLUEZ_CONF"
fi

echo "==> Configuring WirePlumber for HFP"
mkdir -p /etc/wireplumber/wireplumber.conf.d
cp "$SCRIPT_DIR/99-hfp-audio.conf" /etc/wireplumber/wireplumber.conf.d/
chmod 644 /etc/wireplumber/wireplumber.conf.d/99-hfp-audio.conf

echo "==> Enabling services"
systemctl enable bluetooth
systemctl restart bluetooth
# WirePlumber runs in the service user's session, not root's — enable it there.
sudo -u "$SERVICE_USER" \
    XDG_RUNTIME_DIR="/run/user/$(id -u "$SERVICE_USER")" \
    systemctl --user enable wireplumber 2>/dev/null || true

echo "==> Installing Python package"
# Install into a virtual environment in the repo
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"

echo "==> Installing Hermes call-awareness plugin"
HERMES_PLUGIN_DIR="$HOME/.hermes/plugins/hfp-call-awareness"
mkdir -p "$HERMES_PLUGIN_DIR"
cp "$REPO_DIR/hermes_plugin/plugin.yaml" "$HERMES_PLUGIN_DIR/"
cp "$REPO_DIR/hermes_plugin/__init__.py" "$HERMES_PLUGIN_DIR/"
cp "$REPO_DIR/hermes_plugin/hooks.py"   "$HERMES_PLUGIN_DIR/"
echo "    → Installed to $HERMES_PLUGIN_DIR"
echo "    → Restart Hermes to activate (or add to config.yaml: plugins: [hfp-call-awareness])"

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
echo "║  2. Run the MCP server:                                  ║"
echo "║     .venv/bin/hfp-mcp-server                             ║"
echo "║                                                          ║"
echo "║  3. Add to your Hermes config.yaml (see README.md):      ║"
echo "║     mcp_servers: [hfp-mcp]                               ║"
echo "║     plugins: [hfp-call-awareness]                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
