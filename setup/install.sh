#!/usr/bin/env bash
# Idempotent Raspberry Pi OS Bookworm/Trixie installation for hfp-mcp.
set -euo pipefail
umask 077
WITH_GEMINI=false
CHECK_ONLY=false
for option in "$@"; do
    case "$option" in
        --with-gemini) WITH_GEMINI=true ;;
        --check) CHECK_ONLY=true ;;
        *) echo "Usage: sudo bash setup/install.sh [--with-gemini] [--check]" >&2; exit 2 ;;
    esac
done

if ! $CHECK_ONLY && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo: sudo bash setup/install.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
if ! USER_ENTRY="$(getent passwd "$SERVICE_USER")"; then
    echo "ERROR: SERVICE_USER '$SERVICE_USER' does not exist" >&2
    exit 1
fi
USER_HOME="$(printf '%s' "$USER_ENTRY" | cut -d: -f6)"
SERVICE_UID="$(id -u "$SERVICE_USER")"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
USER_CONFIG_DIR="$USER_HOME/.config"
USER_SYSTEMD_DIR="$USER_CONFIG_DIR/systemd/user"
USER_ENV_FILE="$USER_CONFIG_DIR/hfp-mcp.env"
USER_STATE_DIR="$USER_HOME/.local/state/hfp-mcp"

echo "==> Checking host prerequisites before changes"
python3 "$SCRIPT_DIR/host_config.py" --repo "$REPO_DIR" --user "$SERVICE_USER"
if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$SERVICE_USER" -- test -r "$REPO_DIR/src/hfp_mcp/server.py"
fi
if $CHECK_ONLY; then
    exit 0
fi

BACKUP_DIR="$(python3 "$SCRIPT_DIR/host_config.py" --repo "$REPO_DIR" \
    --user "$SERVICE_USER" --backup-dir /var/lib/hfp-mcp/backups)"
echo "==> Configuration backup: $BACKUP_DIR"
trap 'echo "Installation failed. Configuration backup: $BACKUP_DIR. See docs/maintenance.md for recovery." >&2' ERR

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y \
    bluez ffmpeg libsoxr-dev build-essential cmake pkg-config python3-dev python3-numpy \
    python3-dbus python3-gi python3-gi-cairo gir1.2-glib-2.0 \
    libdbus-1-dev libglib2.0-dev libbluetooth-dev libcairo2-dev \
    python3-pip python3-venv

echo "==> Granting the service account Bluetooth access"
getent group bluetooth >/dev/null || groupadd --system bluetooth
usermod -aG bluetooth "$SERVICE_USER"

echo "==> Installing narrow D-Bus policy and BlueZ configuration"
sed "s/__SERVICE_USER__/$SERVICE_USER/g" "$SCRIPT_DIR/bluetooth-policy.conf" \
    > /etc/dbus-1/system.d/hfp-mcp.conf
chmod 644 /etc/dbus-1/system.d/hfp-mcp.conf
python3 "$SCRIPT_DIR/update_bluez_main_conf.py" /etc/bluetooth/main.conf
systemctl reload dbus.service || true
systemctl enable bluetooth.service
systemctl restart bluetooth.service

echo "==> Preventing WirePlumber from competing for the HFP HF profile"
WP_PACKAGE_VERSION="$(dpkg-query -W -f='${Version}' wireplumber 2>/dev/null || true)"
if [ -n "$WP_PACKAGE_VERSION" ] && dpkg --compare-versions "$WP_PACKAGE_VERSION" lt 0.5; then
    install -d -m 755 /etc/wireplumber/bluetooth.lua.d
    install -m 644 "$SCRIPT_DIR/90-hfp-mcp.lua" \
        /etc/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua
    rm -f /etc/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf
    rm -f "$USER_CONFIG_DIR/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf"
    echo "    → Installed WirePlumber 0.4 Lua policy"
else
    install -d -m 755 /etc/wireplumber/wireplumber.conf.d
    install -m 644 "$SCRIPT_DIR/90-hfp-mcp.conf" \
        /etc/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf
    rm -f /etc/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua
    rm -f "$USER_CONFIG_DIR/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua"
    echo "    → Installed WirePlumber 0.5 policy"
fi

echo "==> Installing Python package"
python3 -m venv --system-site-packages "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
if $WITH_GEMINI; then
    echo "    → Including Gemini Live optional dependencies"
    "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR[daemon,gemini-live]"
else
    "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR[daemon]"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$REPO_DIR/.venv"

echo "==> Installing private service configuration"
install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
    "$USER_CONFIG_DIR" "$USER_SYSTEMD_DIR" "$USER_STATE_DIR"
REPO_DIR_ESCAPED="$(printf '%s' "$REPO_DIR" | sed 's/[#&]/\\&/g')"
sed "s#__REPO_DIR__#$REPO_DIR_ESCAPED#g" "$SCRIPT_DIR/hfp-mcp.service" \
    > "$USER_SYSTEMD_DIR/hfp-mcp.service"
chown "$SERVICE_USER:$SERVICE_GROUP" "$USER_SYSTEMD_DIR/hfp-mcp.service"
chmod 644 "$USER_SYSTEMD_DIR/hfp-mcp.service"

if [ -f "$USER_ENV_FILE" ]; then
    "$REPO_DIR/.venv/bin/python" "$SCRIPT_DIR/render_hfp_env.py" \
        --migrate-env "$USER_ENV_FILE" \
        --sync-token-file "$USER_STATE_DIR/control.token"
else
    "$REPO_DIR/.venv/bin/python" "$SCRIPT_DIR/render_hfp_env.py" > "$USER_ENV_FILE"
    "$REPO_DIR/.venv/bin/python" "$SCRIPT_DIR/render_hfp_env.py" \
        --migrate-env "$USER_ENV_FILE" \
        --sync-token-file "$USER_STATE_DIR/control.token"
fi
chown "$SERVICE_USER:$SERVICE_GROUP" "$USER_ENV_FILE"
chmod 600 "$USER_ENV_FILE"
chown "$SERVICE_USER:$SERVICE_GROUP" "$USER_STATE_DIR/control.token"
chmod 600 "$USER_STATE_DIR/control.token"

# Safely adopt an existing phone only when there is exactly one paired HFP AG.
CONFIGURED_PHONE="$(runuser -u "$SERVICE_USER" -- env HOME="$USER_HOME" \
    "$REPO_DIR/.venv/bin/python" -c \
    'from hfp_mcp.settings import RuntimeConfig; print(RuntimeConfig.load().device_address or "")')"
if [ -z "$CONFIGURED_PHONE" ]; then
    HFP_CANDIDATES=()
    while read -r _device address _name; do
        [ -n "${address:-}" ] || continue
        if bluetoothctl info "$address" 2>/dev/null | grep -qi '0000111f-0000-1000-8000-00805f9b34fb'; then
            HFP_CANDIDATES+=("$address")
        fi
    done < <(bluetoothctl devices Paired 2>/dev/null || true)
    if [ "${#HFP_CANDIDATES[@]}" -eq 1 ]; then
        CONFIGURED_PHONE="${HFP_CANDIDATES[0]}"
        echo "    → Found the only paired HFP phone: $CONFIGURED_PHONE"
    else
        echo "WARNING: configure HFP_PHONE_ADDRESS after enrollment; daemon remains fail-closed" >&2
    fi
fi
if [ -n "$CONFIGURED_PHONE" ]; then
    PHONE_INFO="$(bluetoothctl info "$CONFIGURED_PHONE" 2>/dev/null || true)"
    if ! grep -q 'Paired: yes' <<<"$PHONE_INFO" || \
       ! grep -qi '0000111f-0000-1000-8000-00805f9b34fb' <<<"$PHONE_INFO"; then
        echo "ERROR: configured device $CONFIGURED_PHONE is not a paired HFP Audio Gateway" >&2
        exit 1
    fi
    if ! bluetoothctl trust "$CONFIGURED_PHONE" >/dev/null; then
        echo "ERROR: could not trust configured HFP phone $CONFIGURED_PHONE" >&2
        exit 1
    fi
    runuser -u "$SERVICE_USER" -- env HOME="$USER_HOME" \
        "$REPO_DIR/.venv/bin/python" -c \
        'import sys; from pathlib import Path; from hfp_mcp.enrollment import set_phone_in_service_env; set_phone_in_service_env(Path(sys.argv[1]), sys.argv[2])' \
        "$USER_ENV_FILE" "$CONFIGURED_PHONE"
    echo "    → Trusted and configured HFP phone: $CONFIGURED_PHONE"
fi

echo "==> Enabling and starting the canonical daemon"
loginctl enable-linger "$SERVICE_USER"
systemctl start "user@$SERVICE_UID.service"
for _ in 1 2 3 4 5; do
    [ -S "/run/user/$SERVICE_UID/bus" ] && break
    sleep 1
done
USER_SYSTEMCTL=(runuser -u "$SERVICE_USER" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" systemctl --user)
"${USER_SYSTEMCTL[@]}" daemon-reload
"${USER_SYSTEMCTL[@]}" enable hfp-mcp.service
"${USER_SYSTEMCTL[@]}" stop hfp-mcp.service
"${USER_SYSTEMCTL[@]}" try-restart wireplumber.service >/dev/null 2>&1 || true
sleep 2  # allow WirePlumber/BlueZ profile release callbacks to settle
"${USER_SYSTEMCTL[@]}" restart hfp-mcp.service

if ! runuser -u "$SERVICE_USER" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" \
    "$REPO_DIR/.venv/bin/hfp-mcp" wait-live --timeout 90; then
    echo "ERROR: hfp-mcp failed its post-install liveness check" >&2
    "${USER_SYSTEMCTL[@]}" status hfp-mcp.service --no-pager >&2 || true
    # Some hosts keep user-unit logs only in the system journal. This installer
    # already runs as root, so query it directly for the exact service user.
    journalctl "_SYSTEMD_USER_UNIT=hfp-mcp.service" "_UID=$SERVICE_UID" \
        -n 40 --no-pager >&2 || true
    exit 1
fi

# Installation never leaves the adapter open for pairing.
bluetoothctl discoverable off >/dev/null 2>&1 || true
bluetoothctl pairable off >/dev/null 2>&1 || true
bluetoothctl scan off >/dev/null 2>&1 || true

echo "==> Running deployment diagnostics"
runuser -u "$SERVICE_USER" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" \
    "$REPO_DIR/.venv/bin/hfp-mcp" doctor || true

cat <<EOF

Installation complete.

1. If no phone was adopted, enroll one for three minutes:
   $REPO_DIR/.venv/bin/hfp-mcp enroll --timeout 180
2. Optionally pin HFP_ADAPTER_ADDRESS in $USER_ENV_FILE.
3. Verify:
   $REPO_DIR/.venv/bin/hfp-mcp doctor
4. Optional Hermes integration (no profile is created):
   $REPO_DIR/.venv/bin/python $REPO_DIR/setup/install_hermes.py --home <existing-hermes-home> --python <hermes-venv-python>
   See $REPO_DIR/docs/install.md for the complete first-time setup.
5. Local MCP endpoint: http://127.0.0.1:8000/mcp
   The bearer token is synchronized between the mode-0600 environment and
   private control.token file.

LAN clients must use an SSH tunnel or authenticated TLS reverse proxy. The
daemon deliberately refuses an unauthenticated/plaintext non-loopback bind.
EOF
