# phone-bluetooth-hfp-mcp

A Python MCP server that turns a Raspberry Pi (or any Linux machine with Bluetooth) into a **Bluetooth Hands-Free Unit** — exactly like a car kit.  An Android phone connects over Bluetooth HFP and the server exposes MCP tools for making calls and routing the call audio so an AI agent can do STT/TTS.

## How it works

```
Android Phone (HFP Audio Gateway — has cellular)
        ↕  Bluetooth HFP
Raspberry Pi / Linux (HFP Hands-Free Unit)
├── BlueZ 5         — Bluetooth stack, RFCOMM channel
├── PipeWire        — SCO audio routing (auto when call active)
└── hfp-mcp-server  — MCP server
    ├── dial / hangup via AT commands over RFCOMM
    └── capture / playback via PipeWire SCO nodes
```

The phone sees the Pi as a Bluetooth hands-free device (like a car kit or Windows PC).  Once a call is active, PipeWire creates an audio source (phone mic → Pi) and sink (Pi → phone speaker).  The MCP tools let your AI agent capture audio for STT and inject TTS audio back into the call.

## Requirements

| Component | Minimum |
|-----------|---------|
| OS | Raspberry Pi OS Bookworm, Ubuntu 22.04, or any Debian-based distro |
| Bluetooth | Any BlueZ 5.x adapter (Pi 3/4/Zero 2W built-in) |
| Audio | PipeWire + WirePlumber (default on Bookworm) |
| Python | 3.11+ |
| Phone | Android — paired via standard Bluetooth settings |

## Setup

### 1. Install

```bash
git clone https://github.com/virusz4274/phone-bluetooth-hfp-mcp
cd phone-bluetooth-hfp-mcp
sudo bash setup/install.sh
```

The script installs system packages, configures BlueZ and WirePlumber, and creates a virtualenv. It auto-detects the invoking user from `$SUDO_USER` (so `sudo bash …` works on any account, not just `pi`); override explicitly with `SERVICE_USER=youruser sudo -E bash setup/install.sh`. Re-running is safe — every step is idempotent.

### 2. Pair your phone

**Start the MCP server first** (see step 3), then pair. The server registers the
HFP Hands-Free profile and a pairing agent on startup — the phone only sees the
Pi as a "Phone audio" device while the server is running.

With the server running, pair from the **phone's** Bluetooth settings: scan,
select the Pi (its adapter alias, e.g. `raspberrypi`), and confirm the passkey
on the phone. The Pi auto-accepts. Then trust it so it reconnects automatically:

```bash
bluetoothctl devices            # find the phone's address
# Device AA:BB:CC:DD:EE:FF Your Phone
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

If the phone shows **"incorrect passkey / couldn't pair"**, it's almost always
one of: the server isn't running (no HFP profile advertised), or another service
is holding the adapter (see Troubleshooting → *Pairing fails*).

After trusting, the phone auto-reconnects whenever the server starts.

### 3. Configure your MCP client

There are two deployment models depending on where your AI app runs.

---

#### Model A — Same machine (Pi running Hermes / any MCP client)

The MCP client spawns the server as a child process over **stdio**.

```bash
# Run directly
.venv/bin/hfp-mcp-server

# Or add to your MCP client config (e.g. Claude Desktop)
```

```json
{
  "mcpServers": {
    "hfp-phone": {
      "command": "/home/pi/phone-bluetooth-hfp-mcp/.venv/bin/hfp-mcp-server"
    }
  }
}
```

Hermes `config.yaml`:
```yaml
mcp_servers:
  - name: hfp-mcp
    command: /home/pi/phone-bluetooth-hfp-mcp/.venv/bin/hfp-mcp-server

plugins:
  - hfp-call-awareness        # injects call context into every LLM turn
```

---

#### Model B — Pi as Bluetooth gateway, AI app on another machine (Windows / Mac / Linux)

The MCP server must run on the Pi (it owns the Bluetooth hardware), but the AI app (Hermes, Claude Desktop, etc.) runs on a different machine on the same network.

**On the Pi** — start the server in streamable-http (HTTP) mode:

```bash
.venv/bin/hfp-mcp-server --transport streamable-http
# MCP endpoint:    http://raspberrypi.local:8000/mcp
# Status endpoint: http://raspberrypi.local:8001/status
```

You can customise the ports:
```bash
.venv/bin/hfp-mcp-server --transport streamable-http --port 8000 --status-port 8001
```

**On the remote machine** — point your AI app at the Pi:

Claude Desktop `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "hfp-phone": {
      "url": "http://raspberrypi.local:8000/mcp"
    }
  }
}
```

Hermes `config.yaml`:
```yaml
mcp_servers:
  - name: hfp-mcp
    transport: streamable-http
    url: http://raspberrypi.local:8000/mcp

plugins:
  - hfp-call-awareness
```

Hermes plugin — tell it where to fetch call status from the Pi:
```bash
export HFP_MCP_STATUS_URL=http://raspberrypi.local:8001/status
```

Or set it permanently in your shell profile / systemd environment.

> **Security note:** The streamable-http and status ports are unauthenticated,
> and by default the server accepts any `Host` header (DNS-rebinding protection
> off) so LAN clients can connect — see [Host header / remote clients](#host-header--remote-clients-421-misdirected-request).
> Keep these ports on a private/home LAN. To restrict which hosts may connect,
> pass `--allowed-host`. If you need remote access, use an SSH tunnel:
> ```bash
> ssh -L 8000:localhost:8000 -L 8001:localhost:8001 pi@raspberrypi.local
> ```

## Server launch options

```
hfp-mcp-server [--transport stdio|streamable-http]
               [--host HOST] [--port PORT] [--status-port PORT]
               [--allowed-host HOST[:PORT] ...]
```

| Flag | Default | Applies to | Description |
|------|---------|-----------|-------------|
| `--transport` | `stdio` | both | `stdio` = MCP client spawns the server as a child process (Model A). `streamable-http` = serve over HTTP so remote machines can connect (Model B). |
| `--host` | `0.0.0.0` | http | Bind address. `0.0.0.0` = all interfaces (LAN-reachable); `127.0.0.1` = local only. |
| `--port` | `8000` | http | Port for the MCP endpoint (`/mcp`). |
| `--status-port` | `8001` | http | Port for the plain-JSON `/status` endpoint used by the Hermes plugin. |
| `--allowed-host` | _(none)_ | http | Host header value the server will accept (repeatable). Omit to **disable** DNS-rebinding protection so any LAN client connects; pass one or more to lock down. See [Host header / remote clients](#host-header--remote-clients-421-misdirected-request) below. |

The Bluetooth stack (adapter, pairing agent, HFP profile) initialises at process
startup in **both** transports — you don't need an MCP client connected for the
phone to pair or connect.

**Examples:**

```bash
# Local MCP client (Claude Desktop / Hermes) spawns it — Model A
hfp-mcp-server

# Remote client over HTTP on default ports — Model B
hfp-mcp-server --transport streamable-http
#   MCP:    http://<pi-host>:8000/mcp
#   Status: http://<pi-host>:8001/status

# Custom ports (e.g. if 8000 is taken by Docker/another service)
hfp-mcp-server --transport streamable-http --port 8080 --status-port 8081

# Bind to localhost only (pair with an SSH tunnel for remote access)
hfp-mcp-server --transport streamable-http --host 127.0.0.1

# Lock down to specific hostnames/IPs the clients use to reach the Pi
hfp-mcp-server --transport streamable-http \
    --allowed-host raspberrypi.local:8000 --allowed-host 10.0.0.200:8000
```

> **Tip — check what's using a port:** `sudo ss -tlnp | grep 8000`. A Docker
> `docker-proxy` commonly holds 8000; pick free ports with the flags above.

> The endpoint paths are fixed: `/mcp` (MCP) and `/status` (JSON). The host
> portion is whatever the remote machine uses to reach the Pi — `raspberrypi.local`,
> the alias from `bluetoothctl show` (e.g. `tardis.local`), or the LAN IP.

### Host header / remote clients (`421 Misdirected Request`)

The MCP SDK includes **DNS-rebinding protection** that, by default, only accepts
a `Host` header pointing at `localhost`. A remote client (e.g. Codex on another
machine connecting to `http://10.0.0.200:8000/mcp`) sends `Host: 10.0.0.200:8000`,
which the SDK rejects:

```
WARNING  Invalid Host header: 10.0.0.200:8000
POST /mcp HTTP/1.1" 421 Misdirected Request
```

This server is meant for a **trusted private LAN**, so by default (no
`--allowed-host`) it disables that check and accepts any Host header. To keep the
protection on, list every name/IP clients use to reach the Pi:

```bash
# Allow specific host:port values
--allowed-host raspberrypi.local:8000 --allowed-host 10.0.0.200:8000

# Allow a hostname on any port (':*' wildcard)
--allowed-host 'tardis.local:*'
```

> Unrelated 404s for `/.well-known/oauth-authorization-server` are harmless —
> the client is probing for optional OAuth, which this server doesn't use.

## MCP Tools

| Tool | Description |
|------|-------------|
| `scan_paired_devices()` | List paired phones (HFP Audio Gateway devices) |
| `connect_phone(address)` | Connect to phone by BT address |
| `disconnect_phone()` | Disconnect current phone |
| `dial(number)` | Make an outgoing call, e.g. `"+14155551234"` |
| `hangup()` | End the current call |
| `get_call_status()` | Returns connection state, call state, audio_active |
| `start_audio_capture(session_id)` | Start capturing SCO audio (call must be ACTIVE) |
| `get_audio_chunk(session_id)` | Get next captured PCM chunk as base64 |
| `play_audio(session_id, audio_b64)` | Inject base64 PCM audio into the call (TTS) |
| `stop_audio_capture(session_id)` | Stop capture, free resources |

### Audio format

- **Codec**: CVSD (standard HFP narrow-band)
- **Sample rate**: 8 000 Hz
- **Bit depth**: 16-bit signed PCM
- **Channels**: Mono
- **Chunk size**: ~200 ms (1 600 frames)

### Typical call flow

```
1. scan_paired_devices()          → find phone address
2. connect_phone("AA:BB:CC:DD:EE:FF")
3. get_call_status()              → wait for connection=connected
4. dial("+15551234567")
5. get_call_status()              → poll until call_state=active, audio_active=true
6. start_audio_capture("call1")
7. loop:
     chunk = get_audio_chunk("call1")  → base64 PCM → feed to STT
     tts_audio = <your TTS engine>
     play_audio("call1", tts_audio)    → caller hears your AI
8. hangup()
9. stop_audio_capture("call1")
```

## Troubleshooting

**Pairing fails / "incorrect passkey" / `le-connection-abort-by-local`**
- **Is the server running?** It registers the HFP profile + pairing agent on
  startup. With it stopped, the phone won't see "Phone audio" and pairing fails.
- **Another service holding the adapter.** If something else uses the same
  Bluetooth radio — most commonly **Home Assistant's Bluetooth integration**
  (look for `/org/bleak/...` lines in `journalctl -u bluetooth`) — it contends
  with HFP and aborts the connection. Stop that service while pairing, or give
  this server a **dedicated USB Bluetooth dongle** (let the other service keep
  the built-in adapter). HA reaches BlueZ via a `/run/dbus` mount, not
  `privileged`, so removing that mount or disabling its Bluetooth integration
  also frees the radio.
- Pair from the **phone** side and confirm the passkey there (the Pi uses the
  `DisplayYesNo` agent and auto-accepts).

**`address already in use` when starting in streamable-http mode**
- Another process owns the port (a Docker `docker-proxy` on 8000 is common).
  Check with `sudo ss -tlnp | grep <port>` and start on free ports:
  `--port 8080 --status-port 8081`.

**`br-connection-profile-unavailable` on connect**
- PipeWire/WirePlumber must be running to register the HFP audio endpoint:
  `systemctl --user status pipewire wireplumber` — start them if inactive.

**Phone doesn't appear in scan_paired_devices**
- Ensure phone is paired (`bluetoothctl devices`)
- Ensure phone has "Phone audio" enabled in its Bluetooth settings for the Pi

**Handshake fails**
- Check `journalctl -u bluetooth` for BlueZ errors
- Run `sudo btmon` while connecting to see the raw HFP exchange
- Some phones (especially Samsung) initiate the handshake themselves — this is handled automatically

**Audio nodes not found after call connects**
- Verify PipeWire is running: `systemctl --user status pipewire wireplumber`
- Check the WirePlumber config was installed: `ls /etc/wireplumber/wireplumber.conf.d/`
- Run `pactl list short sources` while a call is active to see the nodes

**Running without root**
- Ensure your user is in the `bluetooth` group: `groups $USER`
- Ensure the D-Bus policy was installed: `ls /etc/dbus-1/system.d/hfp-mcp.conf`
- Re-login or reboot for group changes to take effect

**`pip install` fails building pycairo (`Dependency "cairo" not found`)**
- The venv must reuse system GI packages. The installer now creates it with
  `--system-site-packages` and installs `libcairo2-dev`. If you hit this on an
  older checkout: `sudo apt install libcairo2-dev`, then recreate the venv with
  `python3 -m venv --system-site-packages .venv`.

## Development

```bash
# Run tests (no hardware needed)
.venv/bin/pytest tests/ -v

# Run with debug logging
PYTHONUNBUFFERED=1 .venv/bin/hfp-mcp-server 2>&1 | grep -v DEBUG
```

## Architecture

```
src/hfp_mcp/
├── config.py          AT constants, audio params, D-Bus names
├── state.py           Thread-safe shared state + asyncio queue bridges
├── bluez/
│   ├── manager.py     BlueZ adapter / device management (D-Bus)
│   ├── profile.py     org.bluez.Profile1 — receives RFCOMM fd on connect
│   └── agent.py       org.bluez.Agent1 — auto-accept pairing (headless)
├── hfp/
│   ├── protocol.py    AT command strings + ATParser byte→event
│   ├── handshake.py   HFP 1.8 SLC handshake (HF- and AG-initiated)
│   └── session.py     RFCOMMThread (blocking I/O) + ATEventDispatcher (async)
├── audio/
│   ├── pipewire.py    Locate PipeWire SCO nodes via pactl
│   └── capture.py     parec/pacat capture/playback + AudioManager registry
└── server.py          FastMCP app, lifespan, 10 MCP tools
```

## License

MIT
