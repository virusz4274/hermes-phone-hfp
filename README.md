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

The script installs system packages, configures BlueZ and WirePlumber, and creates a virtualenv.

### 2. Pair your phone

Pair once manually:

```bash
bluetoothctl
> power on
> pairable on
> discoverable on
> scan on
# ... find your phone's address, e.g. AA:BB:CC:DD:EE:FF
> pair AA:BB:CC:DD:EE:FF
> trust AA:BB:CC:DD:EE:FF
> quit
```

After this, the phone will auto-reconnect when the server starts.

### 3. Configure your MCP client

Add to your MCP client config (e.g. Claude Desktop `~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hfp-phone": {
      "command": "/home/pi/phone-bluetooth-hfp-mcp/.venv/bin/hfp-mcp-server"
    }
  }
}
```

Or run directly:

```bash
.venv/bin/hfp-mcp-server
```

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

**Phone doesn't appear in scan_paired_devices**
- Ensure phone is paired (`bluetoothctl paired-devices`)
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
│   └── capture.py     sounddevice capture/playback + AudioManager registry
└── server.py          FastMCP app, lifespan, 10 MCP tools
```

## License

MIT
