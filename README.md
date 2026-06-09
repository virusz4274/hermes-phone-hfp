# phone-bluetooth-hfp-mcp

A Python MCP server that turns a Raspberry Pi (or any Linux machine with Bluetooth) into a **Bluetooth Hands-Free Unit** — exactly like a car kit.  An Android phone connects over Bluetooth HFP and the server exposes MCP tools for making calls and routing the call audio so an AI agent can do STT/TTS.

Future Gemini Flash Live integration notes live in
[FUTURE_GEMINI_LIVE_PLAN.md](FUTURE_GEMINI_LIVE_PLAN.md).

## How it works

```
Android Phone (HFP Audio Gateway — has cellular)
        ↕  Bluetooth HFP
Raspberry Pi / Linux (HFP Hands-Free Unit)
├── BlueZ 5         — Bluetooth stack, RFCOMM channel
└── hfp-mcp-server  — MCP server
    ├── dial / hangup via AT commands over RFCOMM
    └── capture / playback over a direct SCO socket (BTPROTO_SCO)
```

The phone sees the Pi as a Bluetooth hands-free device (like a car kit or Windows PC).  Because this server registers its **own** HFP Hands-Free profile and owns the RFCOMM service-level connection, PipeWire's Bluetooth backend never manages the phone — so the server owns the **SCO audio link itself**: it opens a `BTPROTO_SCO` socket and bridges 8 kHz CVSD PCM in both directions (phone mic → Pi, Pi → phone speaker). No PipeWire/PulseAudio nodes are involved in call audio. The MCP tools let your AI agent capture audio for STT and inject TTS audio back into the call.

## Requirements

| Component | Minimum |
|-----------|---------|
| OS | Raspberry Pi OS Bookworm, Ubuntu 22.04, or any Debian-based distro |
| Bluetooth | BlueZ 5.x adapter that routes **SCO over HCI** (Pi 3/4/Zero 2W built-in works) |
| Audio | None for call audio — SCO is bridged directly over the HCI socket |
| Python | 3.11+ |
| Phone | Android — paired via standard Bluetooth settings |

## Setup

### 1. Install

```bash
git clone https://github.com/virusz4274/phone-bluetooth-hfp-mcp
cd phone-bluetooth-hfp-mcp
sudo bash setup/install.sh
```

The script installs system packages (including `ffmpeg` for Hermes TTS audio
conversion), configures BlueZ, creates a virtualenv, installs the Hermes plugins
for the invoking user, and installs/enables/starts a `systemd --user` service
named `hfp-mcp` for remote `streamable-http` access at boot. It auto-detects the
invoking user from `$SUDO_USER` (so `sudo bash …` works on any account, not just
`pi`); override explicitly with
`SERVICE_USER=youruser sudo -E bash setup/install.sh`. Re-running is safe —
every step is idempotent.

### 2. Pair your phone

**Start the MCP server first**, then pair. The installer starts the `hfp-mcp`
user service automatically; if you are running from source without the service,
start `.venv/bin/hfp-mcp-server` before pairing. The server registers the HFP
Hands-Free profile and a pairing agent on startup — the phone only sees the Pi
as a "Phone audio" device while the server is running.

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
systemctl --user status hfp-mcp
# MCP endpoint:    http://raspberrypi.local:8000/mcp
# Status endpoint: http://raspberrypi.local:8001/status
# Audio streams:   ws://raspberrypi.local:8765/audio/<session_id>?token=...
```

If you are not using the installed service, run it manually:

```bash
.venv/bin/hfp-mcp-server --transport streamable-http \
    --audio-host 0.0.0.0 --audio-public-host raspberrypi.local
```

You can customise the service ports in `~/.config/hfp-mcp.env`, or pass them
directly when running manually:

```bash
.venv/bin/hfp-mcp-server --transport streamable-http \
    --port 8000 --status-port 8001 \
    --audio-host 0.0.0.0 --audio-public-host raspberrypi.local
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

Hermes plugin — tell it where to fetch call status from the Pi. The newer
`HFP_PHONE_STATUS_URL` and older `HFP_MCP_STATUS_URL` names are both accepted:
```bash
export HFP_PHONE_STATUS_URL=http://raspberrypi.local:8001/status
```

Or set it permanently in your shell profile / systemd environment.

> **Security note:** Keep the streamable-http, status, and audio sidecar ports on
> a private/home LAN. MCP accepts any `Host` header by default so LAN clients can
> connect; the audio sidecar uses per-session tokens returned from MCP tool
> results. To restrict which HTTP hosts may connect, pass `--allowed-host`. If you
> need remote access, use an SSH tunnel:
> ```bash
> ssh -L 8000:localhost:8000 -L 8001:localhost:8001 -L 8765:localhost:8765 pi@raspberrypi.local
> ```

## Server launch options

```
hfp-mcp-server [--transport stdio|streamable-http]
               [--host HOST] [--port PORT] [--status-port PORT]
               [--audio-host HOST] [--audio-port PORT]
               [--audio-public-host HOST]
               [--allowed-host HOST[:PORT] ...]
```

| Flag | Default | Applies to | Description |
|------|---------|-----------|-------------|
| `--transport` | `stdio` | both | `stdio` = MCP client spawns the server as a child process (Model A). `streamable-http` = serve over HTTP so remote machines can connect (Model B). |
| `--host` | `0.0.0.0` | http | Bind address. `0.0.0.0` = all interfaces (LAN-reachable); `127.0.0.1` = local only. |
| `--port` | `8000` | http | Port for the MCP endpoint (`/mcp`). |
| `--status-port` | `8001` | http | Port for the plain-JSON `/status` endpoint used by the Hermes plugin. |
| `--audio-host` | `127.0.0.1` | both | Bind address for the realtime WebSocket audio sidecar. Keep loopback for same-machine Hermes; use `0.0.0.0` only on a trusted LAN or behind a tunnel. |
| `--audio-port` | `8765` | both | Port for the realtime WebSocket audio sidecar. |
| `--audio-public-host` | _(derived)_ | both | Host/IP used in returned `stream_url` values when the bind host is not directly usable, e.g. `--audio-host 0.0.0.0 --audio-public-host raspberrypi.local`. |
| `--allowed-host` | _(none)_ | http | Host header value the server will accept (repeatable). Omit to **disable** DNS-rebinding protection so any LAN client connects; pass one or more to lock down. See [Host header / remote clients](#host-header--remote-clients-421-misdirected-request) below. |

The Bluetooth stack (adapter, pairing agent, HFP profile) initialises at process
startup in **both** transports — you don't need an MCP client connected for the
phone to pair or connect.

**Examples:**

```bash
# Local MCP client (Claude Desktop / Hermes) spawns it — Model A
hfp-mcp-server

# Remote client over HTTP on default ports — Model B
hfp-mcp-server --transport streamable-http \
    --audio-host 0.0.0.0 --audio-public-host raspberrypi.local
#   MCP:    http://<pi-host>:8000/mcp
#   Status: http://<pi-host>:8001/status
#   Audio:  ws://<pi-host>:8765/audio/<session_id>?token=...

# Custom ports (e.g. if 8000 is taken by Docker/another service)
hfp-mcp-server --transport streamable-http \
    --port 8080 --status-port 8081 \
    --audio-host 0.0.0.0 --audio-public-host raspberrypi.local

# Bind to localhost only (pair with an SSH tunnel for remote access)
hfp-mcp-server --transport streamable-http --host 127.0.0.1

# Lock down to specific hostnames/IPs the clients use to reach the Pi
hfp-mcp-server --transport streamable-http \
    --audio-host 0.0.0.0 --audio-public-host raspberrypi.local \
    --allowed-host raspberrypi.local:8000 --allowed-host 10.0.0.200:8000
```

> **Tip — check what's using a port:** `sudo ss -tlnp | grep 8000`. A Docker
> `docker-proxy` commonly holds 8000; pick free ports with the flags above.

> The endpoint paths are fixed: `/mcp` (MCP) and `/status` (JSON). The host
> portion is whatever the remote machine uses to reach the Pi — `raspberrypi.local`,
> the alias from `bluetoothctl show` (e.g. `tardis.local`), or the LAN IP.

### Run as a systemd service

`setup/install.sh` installs, enables, and starts a **user** systemd service named
`hfp-mcp` and enables linger for the install user, so the server starts at boot
without an interactive login. The service is for `streamable-http` only. `stdio`
mode is not a daemon: a local MCP client starts it over stdin/stdout, and it
exits when that pipe closes.

Manage the service as the install user:

```bash
systemctl --user start hfp-mcp
systemctl --user stop hfp-mcp
systemctl --user restart hfp-mcp
systemctl --user status hfp-mcp
journalctl --user -u hfp-mcp -f
```

Edit `~/.config/hfp-mcp.env` to change service ports or add flags:

```bash
HFP_MCP_OPTS="--port 8000 --status-port 8001 --audio-host 0.0.0.0 --audio-public-host raspberrypi.local"
```

After editing the env file, restart the service:

```bash
systemctl --user restart hfp-mcp
```

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
| `connect_and_wait(address, timeout_seconds=15)` | Connect and wait until HFP is ready |
| `disconnect_phone()` | Disconnect current phone |
| `dial(number)` | Make an outgoing call, e.g. `"+14155551234"` |
| `dial_and_wait(number, timeout_seconds=30)` | Dial and wait until call audio is active |
| `answer_call()` | Answer an incoming call |
| `hangup()` | End the current call |
| `get_call_status()` | Returns connection, call state, phone audio readiness, and SCO bridge state |
| `get_phone_context()` | Agent-friendly call summary with a recommended next action |
| `start_audio_stream(session_id)` | Start/reuse SCO audio and return a duplex WebSocket for realtime STT/TTS |
| `ensure_audio_stream(session_id="active-call")` | Start/reuse realtime audio with a stable default session id |
| `play_audio_file(audio_file, session_id="active-call", tail_ms=1000)` | Convert and play a server-local audio file into active call audio |
| `dial_and_play_audio_file(number, audio_file, session_id=None, timeout_seconds=30, hangup_after=false, tail_ms=1000)` | Dial or attach to active call, then play a server-local audio file |
| `start_audio_capture(session_id)` | Deprecated/diagnostic: start SCO audio for legacy base64 chunk tools |
| `get_audio_chunk(session_id)` | Deprecated/diagnostic: get next captured PCM chunk as base64 |
| `play_audio(session_id, audio_b64)` | Deprecated/diagnostic: queue raw base64 PCM bytes into the call |
| `stop_audio_capture(session_id)` | Stop the SCO audio session and free resources |
| `cleanup_audio_sessions()` | Stop all SCO sessions and clear stale bridge state |

### MCP control-plane optimisations

MCP remains the control plane for call setup, teardown, and status. The realtime
audio path is intentionally outside MCP: use the WebSocket returned by
`start_audio_stream()` / `ensure_audio_stream()` for STT/TTS audio.

The high-level tools reduce repeated agent polling:

- `connect_and_wait()` wraps `connect_phone()` plus connection-state waiting.
- `dial_and_wait()` wraps `dial()` plus call/audio readiness waiting.
- `ensure_audio_stream()` opens or reuses the realtime audio stream with a
  stable default session id.
- `play_audio_file()` accepts normal audio files on the MCP server host, converts
  them to HFP-compatible 8 kHz mono PCM with `ffmpeg`, and queues playback inside
  MCP without a huge base64 payload or client-side WebSocket connection.
- `dial_and_play_audio_file()` is the one-shot reminder/greeting helper: it
  dials when idle, attaches when a call is already active, plays the file, and
  can optionally hang up.
- `get_phone_context()` returns an agent-friendly summary and recommended next
  action so models do not have to infer the workflow from raw state fields.

The older base64 audio chunk tools are deprecated for normal agent use. They
remain available for compatibility and diagnostics, but they are not the
preferred realtime path.

### Audio format

- **Codec**: CVSD (standard HFP narrow-band)
- **Sample rate**: 8 000 Hz
- **Bit depth**: 16-bit signed PCM
- **Channels**: Mono
- **Chunk size**: ~200 ms (1 600 frames)
- **Realtime stream frame size**: 40 ms binary PCM frames over WebSocket

### Realtime audio sidecar

MCP is the control plane for call actions. Realtime audio should use the
WebSocket sidecar returned by `start_audio_stream(session_id)`, not repeated
large `play_audio` / `get_audio_chunk` tool calls.

`start_audio_stream("call1")` opens the SCO audio session if needed and returns:

```json
{
  "ok": true,
  "transport": "websocket",
  "stream_url": "ws://127.0.0.1:8765/audio/call1?token=...",
  "audio": {
    "encoding": "pcm_s16le",
    "sample_rate_hz": 8000,
    "channels": 1,
    "frame_ms": 40,
    "frame_bytes": 640
  }
}
```

Connect to `stream_url` and exchange binary messages:

- Server → client: caller microphone PCM, suitable for STT or a live voice model.
- Client → server: TTS/model PCM, injected into the phone call.

The legacy base64 tools remain useful for compatibility and short diagnostics,
but they are not the preferred path for realtime agents such as Hermes.
`play_audio()` does not accept an audio file path or MP3/WAV data directly; it
expects base64-encoded raw 8 kHz, signed 16-bit, mono PCM bytes. For normal
one-shot AI speech playback, prefer `play_audio_file()` or
`dial_and_play_audio_file()`. For live duplex agents, use the WebSocket stream
returned by `ensure_audio_stream()` or the Hermes phone platform TTS bridge.

### Hermes phone platform plugin

The installer also copies a repo-local Hermes platform plugin to
`~/.hermes/plugins/hfp-phone`. It turns this gateway into a Hermes messaging
platform named `hfp_phone`: incoming phone audio is transcribed through Hermes
STT, Hermes responses are synthesized through Hermes TTS, converted to 8 kHz
mono PCM with `ffmpeg`, and played back into the cellular call over the audio
WebSocket.

Hermes does not replace the MCP server. The `hfp-phone` platform has its own
Hermes integration layer, but it uses the underlying `hfp-mcp-server` MCP tools
for call control (`dial_and_wait`, `answer_call`, `hangup`,
`ensure_audio_stream`, etc.). The MCP server can run without Hermes; Hermes
cannot place or answer HFP calls unless the MCP server is running and reachable.
Set `HFP_PHONE_MCP_URL` to the MCP endpoint on whichever machine owns Bluetooth
HFP. If Hermes runs on the same machine as the installed service, use
`http://127.0.0.1:8000/mcp`; if Hermes runs elsewhere, use the Pi hostname or
LAN IP.

The installer installs Hermes plugins only for `SERVICE_USER` on the machine
where it runs. If Hermes runs on a different host, copy or install the `hfp-phone`
plugin on that Hermes host and set `HFP_PHONE_MCP_URL` to point at the Pi.

Minimal `.env` values for the machine running `hermes gateway`:

```bash
HFP_PHONE_MCP_URL=http://raspberrypi.local:8000/mcp
HFP_PHONE_STATUS_URL=http://raspberrypi.local:8001/status
HFP_PHONE_AUTO_ANSWER=true
HFP_PHONE_OWNER_NUMBER=+15551234567
HFP_PHONE_HOME_CHANNEL=+15551234567
```

Use `.env` for connection/bootstrap values and secrets. Prefer `~/.hermes/config.yaml`
for policy values such as caller roles and restart-notification behavior because
`hermes config set ...` can manage them safely:

```bash
# Avoid gateway restart/shutdown notifications placing HFP phone calls.
hermes config set hfp_phone.gateway_restart_notification false

# Avoid the generic Hermes DM authorization pairing-code flow over phone calls.
hermes config set hfp_phone.unauthorized_dm_behavior ignore

# Current partial role config. These identifiers are matched against the caller
# identifier exposed by HFP status. Today that is usually the Bluetooth device
# address until +CLIP/+CLCC caller-number support is implemented.
hermes config set hfp_phone.admin_callers 22:22:D2:F8:01:7A
hermes config set hfp_phone.trusted_callers +15557654321,+15559876543
```

Equivalent environment variables are also supported when you deliberately want
systemd/shell-managed config instead of Hermes config:

```bash
HFP_PHONE_ALLOWED_CALLERS=22:22:D2:F8:01:7A
HFP_PHONE_ADMIN_CALLERS=+15551234567
HFP_PHONE_TRUSTED_CALLERS=+15557654321,+15559876543
```

Then enable the platform plugin:

```bash
hermes plugins enable hfp-phone
hermes gateway
```

This does **not** give Hermes its own phone number. The paired Android phone is
still the cellular endpoint. Hermes can answer calls routed to that phone, dial
through that phone, and participate in merged calls only when Android routes the
merged-call Bluetooth audio to this HFP device.

For phone reminders, schedule Hermes jobs with `deliver=hfp_phone`. The plugin
uses `HFP_PHONE_HOME_CHANNEL` / `HFP_PHONE_OWNER_NUMBER` as the outbound target:
it dials through the paired Android phone, speaks the reminder through Hermes
TTS, listens for follow-up while the call remains active, and hangs up after
`HFP_PHONE_AUTO_HANGUP_IDLE_SECONDS` seconds of inactivity. With HFP-only
calling, the reminder target must be a different number than the SIM in the
paired gateway phone; a phone cannot dial itself through its own cellular line.

Each cellular call is treated as a separate Hermes session. The plugin uses a
per-call chat id such as `hfp-phone:<device-or-caller>:<call-id>` so a new phone
call from the same Bluetooth device starts fresh context, while all turns inside
the same call stay together. Stable caller metadata is still exposed in
`raw_message.hfp_caller_id`, and the generated per-call id is exposed as
`raw_message.hfp_call_id`.

For direct Hermes sends, `hfp_phone`, `hfp-phone`, `home`, and `owner` resolve to
the configured home/owner number. Explicit phone targets such as
`hfp_phone:+917...`, `hfp-phone:+917...`, `tel:+917...`, or a direct phone-number
chat id are also accepted. For explicit tool use, `hfp_phone_call` accepts either
`message` text or an `audio_file` path; text uses Hermes TTS first, while
`audio_file` skips TTS and is converted for HFP playback.

### Caller permissions status

The HFP phone platform now has a first-pass role model for calls, but true
cellular caller-number identity is still pending. Role config can classify the
caller as `admin`, `trusted`, or `unknown`; however, until `+CLIP`/`+CLCC` caller
ID parsing is added, the only reliable inbound identifier exposed by `/status` is
the connected Bluetooth device address. That address identifies the gateway phone,
not necessarily the person calling the SIM.

For real caller permissions, match roles against cellular phone numbers once
caller ID support is implemented. The Bluetooth device address should be treated
as transport trust only, not as proof that the remote caller is the owner.

Target permission model:

| Level | Who | Allowed actions |
|-------|-----|-----------------|
| Owner/admin | Configured owner/admin phone numbers | Full assistant access, privileged tools, outbound calls, schedules, reminders, and approvals |
| Trusted contacts | Whitelisted numbers | Leave messages, request meetings, create pending reminders/tasks for owner approval |
| Unknown callers | Any non-blocked caller | Basic conversation, voicemail-style messages, callback requests |
| Blocked callers | Denylist | Reject, ignore, or hang up |

Implementation notes:

- Current config keys: `hfp_phone.admin_callers`, `hfp_phone.trusted_callers`,
  `HFP_PHONE_ADMIN_CALLERS`, and `HFP_PHONE_TRUSTED_CALLERS`.
- Current restart/authorization safety keys: `hfp_phone.gateway_restart_notification: false`
  and `hfp_phone.unauthorized_dm_behavior: ignore`.
- Add HFP caller ID support, likely via `AT+CLIP=1` / `+CLIP` and fallback
  `AT+CLCC` parsing, so inbound calls can be mapped to phone numbers instead of
  only the connected Bluetooth device address.
- Gate privileged requests by caller level. Unknown and trusted callers should
  create pending requests instead of directly executing sensitive tools.
- Add optional admin activation by keyword or PIN, for example "admin mode" plus
  a configured code, before allowing high-risk actions.
- Keep an audit log of caller number, transcript, requested action, approval
  status, and executed tool/action.
- Add owner approval flows for meeting requests, reminders, callbacks, and
  anything that modifies calendars/tasks or triggers external actions.

### Typical call flow

```
1. scan_paired_devices()                  → find phone address
2. connect_and_wait("AA:BB:CC:DD:EE:FF")  → wait for connection=connected
3. dial_and_wait("+15551234567")          → wait for call_state=active, audio_active=true
4. ensure_audio_stream("active-call")     → connect STT/TTS or live voice agent to returned WebSocket
5. loop:
     WebSocket receive bytes → feed to STT / live model
     WebSocket send TTS bytes → caller hears your AI
6. hangup()
```

Incoming calls use the same realtime audio path:

```
1. get_phone_context()                  → recommended_next_action=answer_call
2. answer_call()
3. ensure_audio_stream("active-call")
4. loop:
     WebSocket receive bytes → feed to STT / live model
     WebSocket send TTS bytes → caller hears your AI
5. hangup()
```

`audio_active=true` means the phone reports active call audio. `sco_connected=true`
means this server has opened the direct SCO audio bridge. MCP automatically
stops SCO sessions when the phone reports that the call ended. If
`get_phone_context()` ever reports `recommended_next_action=cleanup_audio_sessions`,
call `cleanup_audio_sessions()` to clear stale audio state before starting the
next call.

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

**Phone doesn't appear in scan_paired_devices**
- Ensure phone is paired (`bluetoothctl devices`)
- Ensure phone has "Phone audio" enabled in its Bluetooth settings for the Pi

**Handshake fails**
- Check `journalctl -u bluetooth` for BlueZ errors
- Run `sudo btmon` while connecting to see the raw HFP exchange
- Some phones (especially Samsung) initiate the handshake themselves — this is handled automatically

**`start_audio_capture` fails: "Could not establish SCO audio link"**
- Call audio rides a direct SCO socket, **not** PipeWire — `pactl`/WirePlumber are
  irrelevant here. The one requirement is that the controller routes **SCO over HCI**.
- Confirm the call is still ACTIVE (`get_call_status` → `audio_active: true`) before
  calling `start_audio_capture`; an SCO link can only be opened during an active call.
- Verify SCO reaches the host: run `hciconfig hci0` (or `sudo btmon`) during an active
  call and watch the `sco:` RX/TX counters increment. The Pi's built-in BCM adapter
  routes SCO over HCI out of the box; some USB dongles need their SCO routing enabled.

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
│   └── sco.py         BTPROTO_SCO duplex bridge + AudioManager registry
└── server.py          FastMCP app, lifespan, MCP tools
```

## License

MIT
