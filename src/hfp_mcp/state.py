"""
Thread-safe shared state for the HFP session.

All mutable fields are guarded by a single threading.Lock.
The asyncio event loop and two asyncio.Queue objects bridge the
GLib/RFCOMM threads into asyncio-land.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class CallState(str, Enum):
    IDLE = "idle"
    DIALING = "dialing"
    RINGING = "ringing"    # outgoing ring-back heard
    ACTIVE = "active"
    ENDING = "ending"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    HANDSHAKING = "handshaking"
    CONNECTED = "connected"


@dataclass
class HFPState:
    # -- Bluetooth / RFCOMM --
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    connected_address: Optional[str] = None
    rfcomm_socket: Optional[socket.socket] = None

    # -- HFP session --
    call_state: CallState = CallState.IDLE
    remote_brsf: int = 0
    # name -> [indicator_index (1-based), current_value]
    indicators: dict = field(default_factory=dict)

    # -- Audio --
    audio_active: bool = False
    pipewire_source_name: Optional[str] = None
    pipewire_sink_name: Optional[str] = None

    # -- Call timing --
    call_started_at: Optional[float] = None  # time.monotonic() when call became ACTIVE

    # -- Threading bridges (set during lifespan startup) --
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _asyncio_loop: Optional[asyncio.AbstractEventLoop] = field(default=None, init=False, repr=False)
    # RFCOMM thread → asyncio: parsed AT events (ATResponse / ATResult / ATUnsolicited / None)
    _at_event_queue: Optional[asyncio.Queue] = field(default=None, init=False, repr=False)
    # asyncio → RFCOMM thread: raw AT command bytes to send
    _at_cmd_queue: Optional[asyncio.Queue] = field(default=None, init=False, repr=False)
    # Called (without the lock) after any state mutation; used to write the state file
    _on_change: Optional[Callable[[], None]] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Mutators (all acquire the lock)
    # ------------------------------------------------------------------

    def set_connected(self, address: str, sock: socket.socket) -> None:
        with self._lock:
            self.connected_address = address
            self.rfcomm_socket = sock
            self.connection_state = ConnectionState.HANDSHAKING
        self._notify_change()

    def set_handshake_complete(self) -> None:
        with self._lock:
            self.connection_state = ConnectionState.CONNECTED
        self._notify_change()

    def set_disconnected(self) -> None:
        with self._lock:
            self.connection_state = ConnectionState.DISCONNECTED
            self.connected_address = None
            self.rfcomm_socket = None
            self.call_state = CallState.IDLE
            self.call_started_at = None
            self.audio_active = False
            self.pipewire_source_name = None
            self.pipewire_sink_name = None
            self.indicators = {}
        self._notify_change()

    def set_call_state(self, state: CallState) -> None:
        with self._lock:
            self.call_state = state
            if state == CallState.ACTIVE and self.call_started_at is None:
                self.call_started_at = time.monotonic()
            elif state == CallState.IDLE:
                self.call_started_at = None
        self._notify_change()

    def set_audio_active(self, active: bool) -> None:
        with self._lock:
            self.audio_active = active
        self._notify_change()

    def set_pipewire_devices(self, source: str, sink: str) -> None:
        with self._lock:
            self.pipewire_source_name = source
            self.pipewire_sink_name = sink
        self._notify_change()

    def snapshot(self) -> dict:
        """Return a copy of public state fields (no locks held by caller needed)."""
        with self._lock:
            duration: Optional[float] = None
            if self.call_started_at is not None:
                duration = round(time.monotonic() - self.call_started_at, 1)
            return {
                "connection": self.connection_state.value,
                "connected_address": self.connected_address,
                "call_state": self.call_state.value,
                "audio_active": self.audio_active,
                "pipewire_source": self.pipewire_source_name,
                "pipewire_sink": self.pipewire_sink_name,
                "call_duration_seconds": duration,
            }

    def _notify_change(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Thread-safe bridge helpers
    # ------------------------------------------------------------------

    def post_at_event(self, event) -> None:
        """
        Called from non-asyncio threads (RFCOMM reader).
        Posts an AT event (or None sentinel) into the asyncio queue.
        """
        if self._asyncio_loop is not None and self._at_event_queue is not None:
            self._asyncio_loop.call_soon_threadsafe(
                self._at_event_queue.put_nowait, event
            )
