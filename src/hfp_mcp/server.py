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
  └─ blocking BlueZ D-Bus calls, PipeWire detection, parec/pacat start/stop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator

import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from mcp.server.fastmcp import FastMCP

from .audio.capture import AudioManager
from .audio.pipewire import PipeWireDeviceLocator
from .bluez.agent import HFPAgent, register_agent
from .bluez.manager import BlueZManager
from .bluez.profile import HFPProfile, register_hfp_profile
from .hfp.handshake import HFPHandshaker, HandshakeError
from .hfp.protocol import CMD_ATD, CMD_CHUP
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
        STATE_FILE.write_text(json.dumps(_state.snapshot()))
    except Exception as exc:
        log.debug("State file write failed: %s", exc)


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
    _audio_manager.stop_all()
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

    dispatcher = ATEventDispatcher(_state)
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
    (i.e. phones).  Returns address, name, and whether currently connected.
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
    Returns immediately — use get_call_status to confirm the connection.
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


@mcp.tool()
async def disconnect_phone() -> dict:
    """Disconnect the currently connected phone."""
    if _manager is None:
        return {"ok": False, "error": "Server not initialised"}
    if _state.connection_state == ConnectionState.DISCONNECTED:
        return {"ok": False, "error": "No phone connected"}
    address = _state.connected_address
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _manager.disconnect_device, address)
        _state.set_disconnected()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def dial(number: str) -> dict:
    """
    Make an outgoing call to a phone number (e.g. "+14155551234").
    The phone must already be connected (use connect_phone first).
    Returns immediately; poll get_call_status to track DIALING → ACTIVE.
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
async def hangup() -> dict:
    """End the current call (sends AT+CHUP to the phone)."""
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
    call_state: idle | dialing | ringing | active | ending
    audio_active: true when the SCO audio link is up (call is ACTIVE)
    """
    return _state.snapshot()


@mcp.tool()
async def start_audio_capture(session_id: str) -> dict:
    """
    Begin capturing SCO audio from the phone and prepare the playback stream.

    The call must already be in ACTIVE state (audio_active=true).
    Polls up to 10 s for PipeWire to create the HFP SCO audio nodes.

    session_id: arbitrary string to identify this capture session (e.g. "call1").
    Returns ok=true with PipeWire node names, or ok=false with an error.
    """
    if not _state.audio_active:
        return {
            "ok": False,
            "error": "No active call audio — wait for call_state=active",
        }
    if _audio_manager.get_session(session_id):
        return {"ok": False, "error": f"Session '{session_id}' already exists"}

    address = _state.connected_address
    if not address:
        return {"ok": False, "error": "No connected phone address"}

    locator = PipeWireDeviceLocator()
    loop = asyncio.get_event_loop()
    source, sink = await loop.run_in_executor(
        None, locator.wait_for_hfp_devices, address, 10.0
    )
    if not source or not sink:
        return {
            "ok": False,
            "error": (
                "HFP PipeWire audio nodes not found. "
                "Ensure pipewire-pulse and wireplumber are running and the "
                "headset-roles WirePlumber config includes hfp_hf."
            ),
        }

    _state.set_pipewire_devices(source, sink)
    session = _audio_manager.create_session(session_id, source, sink)
    await loop.run_in_executor(None, session.start)
    return {"ok": True, "source": source, "sink": sink}


@mcp.tool()
async def get_audio_chunk(session_id: str) -> dict:
    """
    Return the next captured audio chunk from the phone microphone.

    Audio format: 8 kHz, 16-bit signed mono PCM, base64-encoded.
    Returns audio_b64=null if no new audio is available yet (buffer empty).
    Call this repeatedly to stream audio to your STT engine.
    """
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    audio_b64 = session.get_chunk_b64()
    return {"ok": True, "audio_b64": audio_b64}


@mcp.tool()
async def play_audio(session_id: str, audio_b64: str) -> dict:
    """
    Play base64-encoded PCM audio into the call (heard by the remote party).

    Audio must be 8 kHz, 16-bit signed mono PCM.
    Use this to inject TTS output from your AI agent into the phone call.

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
    """Stop capturing and playing audio for this session and free resources."""
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _audio_manager.remove_session, session_id)
    return {"ok": True}


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
