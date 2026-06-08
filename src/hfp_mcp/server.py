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
  └─ blocking BlueZ D-Bus calls, PipeWire detection, sounddevice start/stop
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
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

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singletons — initialised during lifespan
# ---------------------------------------------------------------------------

_state = HFPState()
_audio_manager = AudioManager()
_manager: BlueZManager | None = None
_rfcomm_thread: RFCOMMThread | None = None
_dispatcher_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    global _manager, _rfcomm_thread, _dispatcher_task

    loop = asyncio.get_event_loop()

    # Queues MUST be created inside a running event loop (Python ≥ 3.10)
    _state._asyncio_loop = loop
    _state._at_event_queue = asyncio.Queue()
    _state._at_cmd_queue = asyncio.Queue()

    # Initialise D-Bus with GLib integration BEFORE creating the SystemBus
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    _manager = BlueZManager(bus)
    _manager.find_adapter()
    _manager.set_powered(True)
    _manager.set_pairable(True)

    # Pairing agent (auto-accept, headless mode)
    _agent = HFPAgent(bus)
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

    _profile = HFPProfile(bus, _on_new_connection, _on_request_disconnection, _on_release)
    register_hfp_profile(bus)

    # Start GLib MainLoop in background thread (owns all D-Bus I/O)
    glib_loop = GLib.MainLoop()
    glib_thread = threading.Thread(
        target=glib_loop.run, daemon=True, name="glib-mainloop"
    )
    glib_thread.start()
    log.info("GLib MainLoop started")

    try:
        yield
    finally:
        _audio_manager.stop_all()
        glib_loop.quit()
        log.info("GLib MainLoop stopped")


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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run("stdio")
