"""
FastMCP server: HFP phone calling over Bluetooth.

Threading model
───────────────
Main thread   asyncio event loop (FastMCP / anyio)
  ├─ MCP stdio I/O
  ├─ Tool coroutines (dial, hangup, …)
  ├─ _run_handshake_and_session() coroutine
  └─ ATEventDispatcher asyncio Task

Thread "glib-mainloop"
  └─ GLib.MainLoop → dbus-python dispatches D-Bus signals
        └─ HFPProfile.NewConnection → loop.call_soon_threadsafe(...)

Thread "rfcomm-io"  (daemon)
  ├─ blocking socket.recv → loop.call_soon_threadsafe(queue.put_nowait, event)
  └─ writer: asyncio.run_coroutine_threadsafe(queue.get) → socket.sendall

Thread pool (asyncio run_in_executor)
  └─ blocking BlueZ D-Bus calls, SCO socket connect/teardown

Thread "sco-<session>"  (daemon, per audio session)
  └─ duplex SCO bridge: recv phone mic → capture ring; send playback → phone
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator

import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from mcp.server.fastmcp import FastMCP

from .audio.sco import AudioManager, SCOAudioError
from .audio.sidecar import AudioStreamServer
from .bluez.agent import HFPAgent, register_agent
from .bluez.manager import BlueZManager
from .bluez.profile import HFPProfile, register_hfp_profile
from .config import AUDIO_STREAM_HOST, AUDIO_STREAM_PORT
from .hfp.handshake import HFPHandshaker, HandshakeError
from .hfp.protocol import CMD_ATA, CMD_ATD, CMD_CHUP
from .hfp.session import ATEventDispatcher, RFCOMMThread
from .state import CallState, ConnectionState, HFPState
from .transport import apply_network_settings

log = logging.getLogger(__name__)

STATE_FILE = Path("/tmp/hfp-mcp-state.json")

# ---------------------------------------------------------------------------
# Singletons — initialised during lifespan
# ---------------------------------------------------------------------------

_state = HFPState()
_audio_manager = AudioManager()
_audio_stream_server: AudioStreamServer | None = None
_manager: BlueZManager | None = None
_rfcomm_thread: RFCOMMThread | None = None
_dispatcher_task: asyncio.Task | None = None

# Bluetooth stack — initialised once at process startup (see _start_bluetooth_stack)
_glib_loop: "GLib.MainLoop | None" = None
_agent_ref = None
_profile_ref = None
_bt_initialized = False
_bt_init_lock: "asyncio.Lock | None" = None
_http_mode = False


def _write_state_file() -> None:
    """Write a JSON snapshot of HFP state to STATE_FILE for the Hermes plugin."""
    try:
        tmp = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")
        tmp.write_text(json.dumps(_state.snapshot()))
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log.debug("State file write failed: %s", exc)


def _refresh_sco_connected() -> None:
    _state.set_sco_connected(_audio_manager.has_sessions())


async def _cleanup_all_audio_sessions() -> int:
    loop = asyncio.get_running_loop()
    stopped = await loop.run_in_executor(None, _audio_manager.stop_all)
    _refresh_sco_connected()
    return stopped


def _schedule_call_end_audio_cleanup() -> None:
    loop = _state._asyncio_loop
    if loop is None or loop.is_closed():
        stopped = _audio_manager.stop_all()
        _state.set_sco_connected(False)
        if stopped:
            log.info("Stopped %d SCO audio session(s) after call ended", stopped)
        return

    def _create_cleanup_task() -> None:
        asyncio.create_task(_cleanup_all_audio_sessions(), name="audio-cleanup")

    loop.call_soon_threadsafe(_create_cleanup_task)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _start_bluetooth_stack() -> None:
    """
    Initialise the D-Bus / BlueZ side: adapter, pairing agent, HFP profile,
    and the GLib main loop that dispatches D-Bus signals.

    Idempotent — safe to call from both the stdio lifespan and the HTTP app
    startup hook. The work runs exactly once, on whichever event loop is
    serving MCP tool calls (the asyncio queues must bind to that loop).
    """
    global _manager, _glib_loop, _agent_ref, _profile_ref
    global _bt_initialized, _bt_init_lock

    if _bt_init_lock is None:
        _bt_init_lock = asyncio.Lock()
    async with _bt_init_lock:
        if _bt_initialized:
            return

        loop = asyncio.get_running_loop()

        # Queues MUST be created inside a running event loop (Python ≥ 3.10)
        _state._asyncio_loop = loop
        _state._at_event_queue = asyncio.Queue()
        _state._at_cmd_queue = asyncio.Queue()
        _state._on_change = _write_state_file
        _write_state_file()  # write initial disconnected state

        # Initialise D-Bus with GLib integration BEFORE creating the SystemBus
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        _manager = BlueZManager(bus)
        _manager.find_adapter()
        _manager.set_powered(True)
        _manager.set_pairable(True)

        # Pairing agent (Pi auto-accepts; phone shows passkey to user)
        _agent_ref = HFPAgent(bus)
        register_agent(bus)

        # HFP profile — BlueZ calls NewConnection when the phone connects
        def _on_new_connection(address: str, sock, props: dict) -> None:
            _state.set_connected(address, sock)
            # create_task is safe here: call_soon_threadsafe runs the callback
            # inside the asyncio event loop, so create_task has a running loop.
            loop.call_soon_threadsafe(
                asyncio.create_task,
                _run_handshake_and_session(address),
            )

        def _on_request_disconnection(address: str) -> None:
            log.info("Phone requested disconnection: %s", address)
            _state.set_disconnected()
            _schedule_call_end_audio_cleanup()

        def _on_release() -> None:
            log.warning("HFP profile released by bluetoothd")

        _profile_ref = HFPProfile(
            bus, _on_new_connection, _on_request_disconnection, _on_release
        )
        register_hfp_profile(bus)

        # Start GLib MainLoop in background thread (owns all D-Bus I/O)
        _glib_loop = GLib.MainLoop()
        threading.Thread(
            target=_glib_loop.run, daemon=True, name="glib-mainloop"
        ).start()
        log.info("GLib MainLoop started")

        _bt_initialized = True


def _stop_bluetooth_stack() -> None:
    """Tear down the GLib loop and audio. Called once at process shutdown."""
    global _glib_loop
    if _audio_stream_server is not None:
        _audio_stream_server.stop()
    _audio_manager.stop_all()
    _state.set_sco_connected(False)
    if _glib_loop is not None:
        _glib_loop.quit()
        _glib_loop = None
    STATE_FILE.unlink(missing_ok=True)
    log.info("GLib MainLoop stopped")


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    # Runs immediately in stdio mode (one session at launch); in HTTP mode it
    # also runs per MCP session, but _start_bluetooth_stack is guarded so the
    # real work happens exactly once (the HTTP app startup hook calls it first).
    await _start_bluetooth_stack()
    try:
        yield
    finally:
        # HTTP mode tears down via the app shutdown hook (not per-session).
        if not _http_mode:
            _stop_bluetooth_stack()


async def _run_handshake_and_session(address: str) -> None:
    global _rfcomm_thread, _dispatcher_task

    # RFCOMM thread must start BEFORE the handshake: the handshaker writes
    # commands to _at_cmd_queue and reads responses from _at_event_queue, and
    # only RFCOMMThread moves bytes between those queues and the real socket.
    _rfcomm_thread = RFCOMMThread(_state)
    _rfcomm_thread.start()

    try:
        await HFPHandshaker(_state).run()
        log.info("HFP connected to %s", address)
    except HandshakeError as exc:
        log.error("Handshake failed: %s", exc)
        _rfcomm_thread.stop()
        _state.set_disconnected()
        return

    dispatcher = ATEventDispatcher(_state, _schedule_call_end_audio_cleanup)
    _dispatcher_task = asyncio.create_task(dispatcher.run(), name="at-dispatcher")


# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("HFP Phone Controller", lifespan=lifespan)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def scan_paired_devices() -> list[dict]:
    """
    List Bluetooth devices paired with this machine that support HFP
    (i.e. phones). Returns address, name, and whether currently connected.

    Recommended workflow:
    1. Use this once to find the phone address.
    2. Use connect_and_wait(address) to connect and wait for HFP readiness.
    """
    if _manager is None:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _manager.get_paired_hfp_devices)


@mcp.tool()
async def connect_phone(address: str) -> dict:
    """
    Connect to a paired Android phone by Bluetooth address (AA:BB:CC:DD:EE:FF).
    Triggers the HFP profile connection; the handshake runs automatically.

    Low-level tool: returns immediately. Agents should usually prefer
    connect_and_wait(address), which avoids repeated get_call_status polling.
    """
    if _manager is None:
        return {"ok": False, "error": "Server not initialised"}
    if _state.connection_state != ConnectionState.DISCONNECTED:
        return {
            "ok": False,
            "error": f"Already connected to {_state.connected_address}",
        }
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _manager.connect_device, address)
        return {"ok": True, "message": f"Connection initiated to {address}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _wait_for_status(predicate, timeout_seconds: float, interval: float = 0.25) -> dict:
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout_seconds)
    last = _state.snapshot()
    while True:
        if predicate(last):
            return {"ok": True, "status": last}
        if asyncio.get_event_loop().time() >= deadline:
            return {
                "ok": False,
                "error": f"Timed out after {timeout_seconds:g}s",
                "status": last,
            }
        await asyncio.sleep(interval)
        last = _state.snapshot()


@mcp.tool()
async def connect_and_wait(address: str, timeout_seconds: float = 15.0) -> dict:
    """
    Connect to a paired phone and wait until the HFP service-level connection is
    ready. This collapses connect_phone + repeated get_call_status polling.

    Use this before dial_and_wait(), answer_call(), or ensure_audio_stream().
    Returns ok=true with status when connection=connected.
    """
    if _state.connection_state == ConnectionState.CONNECTED:
        return {"ok": True, "status": _state.snapshot()}

    result = await connect_phone(address)
    if not result.get("ok"):
        return result

    return await _wait_for_status(
        lambda s: s["connection"] == ConnectionState.CONNECTED.value,
        timeout_seconds,
    )


@mcp.tool()
async def disconnect_phone() -> dict:
    """
    Disconnect the currently connected phone.

    This also clears call/audio state. Use hangup() first if a call is active
    and you want the phone call ended cleanly before Bluetooth disconnects.
    """
    if _manager is None:
        return {"ok": False, "error": "Server not initialised"}
    if _state.connection_state == ConnectionState.DISCONNECTED:
        return {"ok": False, "error": "No phone connected"}
    address = _state.connected_address
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _manager.disconnect_device, address)
        await _cleanup_all_audio_sessions()
        _state.set_disconnected()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def dial(number: str) -> dict:
    """
    Make an outgoing call to a phone number (e.g. "+14155551234").
    The phone must already be connected.

    Low-level tool: returns immediately. Agents should usually prefer
    dial_and_wait(number), which waits until call_state=active and
    audio_active=true before returning.
    """
    if _state.connection_state != ConnectionState.CONNECTED:
        return {"ok": False, "error": "Phone not connected (HFP not ready)"}
    if _state.call_state != CallState.IDLE:
        return {"ok": False, "error": f"Already in call state: {_state.call_state.value}"}
    cmd = CMD_ATD.format(number=number).encode("ascii")
    await _state._at_cmd_queue.put(cmd)
    _state.set_call_state(CallState.DIALING)
    return {"ok": True, "dialing": number}


@mcp.tool()
async def dial_and_wait(number: str, timeout_seconds: float = 30.0) -> dict:
    """
    Dial a number and wait until the call becomes active and call audio is
    reported ready. This collapses dial + repeated get_call_status polling.

    Recommended outbound-call workflow:
    1. connect_and_wait(address)
    2. dial_and_wait(number)
    3. ensure_audio_stream("active-call")
    4. Use the returned WebSocket for STT/TTS audio.
    5. hangup() when done.
    """
    result = await dial(number)
    if not result.get("ok"):
        return result

    return await _wait_for_status(
        lambda s: s["call_state"] == CallState.ACTIVE.value
        and bool(s["audio_active"]),
        timeout_seconds,
    )


@mcp.tool()
async def answer_call() -> dict:
    """
    Answer an incoming call (sends ATA to the phone).

    Recommended incoming-call workflow:
    1. get_phone_context() reports incoming_call=true or recommended_next_action=answer_call.
    2. answer_call()
    3. Wait for get_phone_context() to show call_active=true/audio_active=true if needed.
    4. ensure_audio_stream("active-call")
    5. Use the returned WebSocket for STT/TTS audio.
    """
    if _state.connection_state != ConnectionState.CONNECTED:
        return {"ok": False, "error": "Phone not connected (HFP not ready)"}
    if _state.call_state not in (CallState.INCOMING, CallState.RINGING):
        return {
            "ok": False,
            "error": f"No incoming call to answer (state: {_state.call_state.value})",
        }
    await _state._at_cmd_queue.put(CMD_ATA.encode("ascii"))
    return {"ok": True}


@mcp.tool()
async def hangup() -> dict:
    """
    End the current call (sends AT+CHUP to the phone).

    Use this after outbound calls, incoming calls, reminders, or live voice
    sessions. Call stop_audio_capture(session_id) if a legacy/base64 capture
    session is still open.
    """
    if _state.call_state == CallState.IDLE:
        return {"ok": False, "error": "No active call"}
    await _state._at_cmd_queue.put(CMD_CHUP.encode("ascii"))
    _state.set_call_state(CallState.ENDING)
    return {"ok": True}


@mcp.tool()
async def get_call_status() -> dict:
    """
    Return the current Bluetooth connection and call state.

    connection: disconnected | handshaking | connected
    call_state: idle | incoming | dialing | ringing | active | ending
    audio_active: true when the phone reports active call audio
    sco_connected: true when this server has opened the SCO audio bridge

    For agent decision-making, prefer get_phone_context(), which includes
    booleans and recommended_next_action.
    """
    return _state.snapshot()


@mcp.tool()
async def get_phone_context() -> dict:
    """
    Return an agent-friendly summary of the phone/call state with booleans and a
    recommended next action. Prefer this when an LLM needs to choose a call step.

    Typical recommended_next_action values:
    - connect_phone: no phone is connected; use scan_paired_devices then connect_and_wait.
    - wait: connection/call state is changing.
    - dial: phone is connected and idle; use dial_and_wait.
    - answer_call: incoming call is ringing; use answer_call.
    - start_audio_stream: call is active; use ensure_audio_stream.
    - continue_call: call audio is already active/streaming.
    - cleanup_audio_sessions: call is idle but a stale SCO bridge remains.
    """
    status = _state.snapshot()
    connection = status["connection"]
    call_state = status["call_state"]
    connected = connection == ConnectionState.CONNECTED.value
    call_active = call_state == CallState.ACTIVE.value
    incoming = call_state == CallState.INCOMING.value

    stale_audio_session = call_state == CallState.IDLE.value and status["sco_connected"]

    if connection == ConnectionState.DISCONNECTED.value:
        next_action = "connect_phone"
    elif connection == ConnectionState.HANDSHAKING.value:
        next_action = "wait"
    elif stale_audio_session:
        next_action = "cleanup_audio_sessions"
    elif incoming:
        next_action = "answer_call"
    elif call_active and status["audio_active"] and not status["sco_connected"]:
        next_action = "start_audio_stream"
    elif call_active:
        next_action = "continue_call"
    elif call_state in (CallState.DIALING.value, CallState.RINGING.value, CallState.ENDING.value):
        next_action = "wait"
    else:
        next_action = "dial"

    return {
        **status,
        "ready_to_call": connected and call_state == CallState.IDLE.value and not stale_audio_session,
        "connected": connected,
        "incoming_call": incoming,
        "call_active": call_active,
        "recommended_next_action": next_action,
    }


@mcp.tool()
async def start_audio_capture(session_id: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: open the SCO audio link for legacy base64 audio
    chunk tools.

    The call must already be in ACTIVE state (audio_active=true). This connects a
    Bluetooth SCO socket directly (HF-initiated) and streams 8 kHz CVSD PCM — it
    does not depend on PipeWire.

    session_id: arbitrary string to identify this capture session (e.g. "call1").
    Returns ok=true once the SCO bridge is open, or ok=false with an error.

    Legacy/diagnostic tool. Realtime agents should prefer
    ensure_audio_stream(), which returns a duplex WebSocket and avoids large
    repeated MCP base64 tool calls.
    """
    if _audio_manager.get_session(session_id):
        return {"ok": False, "error": f"Session '{session_id}' already exists"}

    result = await _open_audio_session(session_id)
    if not result["ok"]:
        return result

    _state.set_sco_connected(True)
    return {"ok": True, "transport": "sco", "mtu": result["mtu"]}


@mcp.tool()
async def start_audio_stream(session_id: str) -> dict:
    """
    Start or reuse a call audio session and return a WebSocket stream endpoint.

    The WebSocket is the realtime audio plane. It sends and receives binary PCM:
    8 kHz, signed 16-bit, mono. Use MCP tools for call control; use this stream
    for STT/TTS or live voice agents. The call must already be ACTIVE.

    Prefer ensure_audio_stream("active-call") unless the client needs a custom
    session id. Do not use get_audio_chunk/play_audio for realtime voice unless
    WebSockets are unavailable.
    """
    global _audio_stream_server

    session = _audio_manager.get_session(session_id)
    if session is None:
        result = await _open_audio_session(session_id)
        if not result["ok"]:
            return result
        session = _audio_manager.get_session(session_id)
        _state.set_sco_connected(True)
    if session is None:
        return {"ok": False, "error": f"No session '{session_id}'"}
    if _audio_stream_server is None:
        return {"ok": False, "error": "Audio stream sidecar is not configured"}

    _audio_stream_server.start()
    token = _audio_stream_server.issue_token(session_id)
    return {
        "ok": True,
        "transport": "websocket",
        "stream_url": _audio_stream_server.stream_url(session_id, token),
        "session_id": session_id,
        "mtu": session.mtu,
        "audio": _audio_stream_server.metadata(),
        "direction": "duplex",
        "message_format": "binary PCM frames in both directions",
    }


@mcp.tool()
async def ensure_audio_stream(session_id: str = "active-call") -> dict:
    """
    Start or reuse the realtime call audio stream using a stable default session
    id. Prefer this for agents that do not care about naming audio sessions.

    Use after dial_and_wait() for outbound calls or after answer_call() once
    audio_active=true. The returned stream_url is the realtime duplex audio
    channel: receive caller PCM for STT and send TTS PCM for playback.
    """
    return await start_audio_stream(session_id)


@mcp.tool()
async def get_audio_chunk(session_id: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: return the next legacy base64 audio chunk from the
    phone microphone.

    Audio format: 8 kHz, 16-bit signed mono PCM, base64-encoded.
    Returns audio_b64=null if no new audio is available yet (buffer empty).
    Legacy/diagnostic tool. Prefer ensure_audio_stream for realtime STT/TTS.
    """
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    audio_b64 = session.get_chunk_b64()
    return {"ok": True, "audio_b64": audio_b64}


@mcp.tool()
async def play_audio(session_id: str, audio_b64: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: queue legacy base64-encoded PCM audio into the call.

    This does NOT accept an audio file path or compressed audio. audio_b64 must
    be base64-encoded raw PCM bytes: 8 kHz, 16-bit signed, mono.
    Legacy/diagnostic tool. Prefer ensure_audio_stream for realtime TTS/live
    voice agents.

    audio_b64: base64-encoded raw PCM bytes.
    Returns bytes_queued on success.
    """
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    try:
        loop = asyncio.get_event_loop()
        n = await loop.run_in_executor(None, session.queue_playback_b64, audio_b64)
        return {"ok": True, "bytes_queued": n}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def stop_audio_capture(session_id: str) -> dict:
    """Stop the SCO audio session and free capture/playback resources."""
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _audio_manager.remove_session, session_id)
    _refresh_sco_connected()
    return {"ok": True}


@mcp.tool()
async def cleanup_audio_sessions() -> dict:
    """
    Stop all SCO audio sessions and refresh bridge state.

    This is normally automatic when the phone reports call end. Use it as a
    recovery action if get_phone_context reports cleanup_audio_sessions.
    """
    stopped = await _cleanup_all_audio_sessions()
    return {"ok": True, "sessions_stopped": stopped}


async def _open_audio_session(session_id: str) -> dict:
    if not _state.audio_active:
        return {
            "ok": False,
            "error": "No active call audio — wait for call_state=active",
        }

    address = _state.connected_address
    if not address:
        return {"ok": False, "error": "No connected phone address"}

    session = _audio_manager.create_session(session_id, address)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, session.start)
    except SCOAudioError as exc:
        _audio_manager.remove_session(session_id)
        return {
            "ok": False,
            "error": (
                f"{exc}. Confirm the call is still active and that the Bluetooth "
                "controller routes SCO over HCI (hciconfig hci0 should show SCO "
                "RX/TX counters during a call)."
            ),
        }
    return {"ok": True, "transport": "sco", "mtu": session.mtu}


# ---------------------------------------------------------------------------
# Status HTTP server (streamable-http / network mode only)
# ---------------------------------------------------------------------------

def _make_status_server(host: str, port: int) -> HTTPServer:
    """
    Tiny HTTP server that serves GET /status → current HFP state as JSON.
    Used by the Hermes plugin when Hermes runs on a different machine to the Pi.
    """
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                body = json.dumps(_state.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_):  # suppress per-request noise
            pass

    return HTTPServer((host, port), _Handler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HFP MCP Server — Bluetooth phone calling for Linux/Raspberry Pi"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help=(
            "MCP transport: 'stdio' (default) for same-machine clients; "
            "'streamable-http' to serve over HTTP so remote machines can connect"
        ),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host for streamable-http transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the streamable-http MCP transport (default: 8000)",
    )
    parser.add_argument(
        "--status-port",
        type=int,
        default=8001,
        help="Port for the /status JSON endpoint used by the Hermes plugin on remote machines (default: 8001, streamable-http mode only)",
    )
    parser.add_argument(
        "--audio-host",
        default=AUDIO_STREAM_HOST,
        help="Bind host for the realtime audio WebSocket sidecar (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--audio-port",
        type=int,
        default=AUDIO_STREAM_PORT,
        help="Port for the realtime audio WebSocket sidecar (default: 8765)",
    )
    parser.add_argument(
        "--audio-public-host",
        default=None,
        help=(
            "Host/IP to place in returned audio WebSocket URLs. Useful when "
            "--audio-host is 0.0.0.0 for remote LAN clients."
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        metavar="HOST[:PORT]",
        default=None,
        help=(
            "Add a Host header value the HTTP transport will accept (repeatable). "
            "Use a 'host:*' pattern to allow any port, e.g. 'raspberrypi.local:*'. "
            "If omitted, DNS-rebinding protection is DISABLED so any LAN client can "
            "connect — fine for a trusted private network. Pass one or more "
            "--allowed-host to lock the server down to specific names/IPs."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    global _audio_stream_server
    _audio_stream_server = AudioStreamServer(
        _audio_manager,
        args.audio_host,
        args.audio_port,
        args.audio_public_host,
    )

    if args.transport == "streamable-http":
        global _http_mode
        _http_mode = True

        # FastMCP.run() does not accept host/port — they must be set on the
        # settings object before run() (see hfp_mcp/transport.py).
        apply_network_settings(mcp.settings, args.host, args.port)

        # The MCP SDK's DNS-rebinding protection only trusts a localhost Host
        # header by default, so remote LAN clients (e.g. Codex on another
        # machine) are rejected with "421 Misdirected Request / Invalid Host
        # header". This server is intended for a trusted private LAN (see the
        # security note in the README). Without --allowed-host we disable the
        # check so any LAN client connects; with it, we lock down to the given
        # names/IPs (supports 'host:*' to allow any port).
        from mcp.server.transport_security import TransportSecuritySettings

        if args.allowed_host:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=args.allowed_host,
                allowed_origins=args.allowed_host,
            )
            log.info("HTTP Host allowlist: %s", ", ".join(args.allowed_host))
        else:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )

        status_srv = _make_status_server(args.host, args.status_port)
        status_thread = threading.Thread(
            target=status_srv.serve_forever,
            daemon=True,
            name="status-http",
        )
        status_thread.start()
        log.info(
            "MCP endpoint:    http://%s:%d/mcp", args.host, args.port
        )
        log.info(
            "Status endpoint: http://%s:%d/status  (set HFP_MCP_STATUS_URL on remote Hermes host)",
            args.host,
            args.status_port,
        )
        log.info(
            "Audio sidecar:   ws://%s:%d/audio/<session_id>",
            args.audio_host,
            args.audio_port,
        )

        # The FastMCP HTTP app's own lifespan only runs the MCP session
        # manager — our Bluetooth stack lives on the per-session server, which
        # would leave BlueZ uninitialised until a client connects. Wrap the app
        # lifespan so the Bluetooth stack comes up at process startup instead.
        import uvicorn

        app = mcp.streamable_http_app()
        session_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def _app_lifespan(starlette_app):
            await _start_bluetooth_stack()
            async with session_lifespan(starlette_app):
                yield
            _stop_bluetooth_stack()

        app.router.lifespan_context = _app_lifespan
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        mcp.run("stdio")
