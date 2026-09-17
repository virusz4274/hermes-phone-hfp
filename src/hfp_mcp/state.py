"""Thread-safe, versioned runtime state for the HFP daemon.

The daemon has several execution domains (GLib/D-Bus, RFCOMM, SCO and
asyncio).  This module is deliberately dependency-free so every domain can
publish state through one reducer without importing the MCP server.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable, Optional


class CallState(str, Enum):
    IDLE = "idle"
    INCOMING = "incoming"
    DIALING = "dialing"
    RINGING = "ringing"
    ACTIVE = "active"
    HELD = "held"
    ENDING = "ending"


class CallDirection(str, Enum):
    UNKNOWN = "unknown"
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    CONNECTED = "connected"
    ERROR = "error"


class SCOState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    FAILED = "failed"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class HFPState:
    """Mutable daemon state guarded by one re-entrant lock.

    The flat attributes remain available to the original server and plugins.
    :meth:`snapshot` additionally emits the stable ``hfp.v1`` nested contract.
    """

    # Bluetooth / RFCOMM
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    connected_address: Optional[str] = None
    connected_name: Optional[str] = None
    adapter_address: Optional[str] = None
    rfcomm_socket: Optional[socket.socket] = None
    connection_generation: int = 0

    # HFP session
    call_state: CallState = CallState.IDLE
    call_direction: CallDirection = CallDirection.UNKNOWN
    call_id: Optional[str] = None
    call_generation: int = 0
    remote_number: Optional[str] = None
    remote_number_source: Optional[str] = None
    remote_name: Optional[str] = None
    caller_role: str = "unknown"
    remote_brsf: int = 0
    indicators: dict[str, list[int]] = field(default_factory=dict)

    # Audio. ``audio_active`` is the legacy HFP call-audio indicator.  Physical
    # readiness is represented exclusively by ``sco_state``/``sco_connected``.
    audio_active: bool = False
    sco_connected: bool = False
    sco_state: SCOState = SCOState.DISCONNECTED
    bridge_state: str = "stopped"
    audio_owner: Optional[str] = None
    audio_stream_id: Optional[str] = None
    audio_client_attached: bool = False
    audio_queue_ms: float = 0.0
    audio_dropped_frames: int = 0
    audio_queue_peak_ms: float = 0.0
    audio_overflow_events: int = 0
    audio_rejected_bytes: int = 0
    audio_accepted_bytes: int = 0
    audio_consumed_bytes: int = 0
    audio_playback_controlled: bool = False
    audio_playback_target_ms: float = 0.0
    audio_playback_underrun_events: int = 0
    audio_playback_underrun_ms: float = 0.0
    audio_capture_queue_ms: float = 0.0
    audio_capture_queue_peak_ms: float = 0.0
    audio_capture_overflow_bytes: int = 0

    # Timing / health
    call_started_at: Optional[float] = None
    call_started_at_utc: Optional[str] = None
    revision: int = 0
    updated_at: str = field(default_factory=_utc_now)
    server_instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    live_ai: dict[str, Any] = field(
        default_factory=lambda: {
            "state": "stopped",
            "provider": None,
            "model": None,
            "session_id": None,
            "pending_tools": 0,
        }
    )
    health: dict[str, str] = field(
        default_factory=lambda: {
            "bluez": "unknown",
            "rfcomm": "stopped",
            "sco": "stopped",
            "http": "unknown",
            "gemini": "disabled",
            "ffmpeg": "unknown",
        }
    )

    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _asyncio_loop: Optional[asyncio.AbstractEventLoop] = field(default=None, init=False, repr=False)
    _at_event_queue: Optional[asyncio.Queue] = field(default=None, init=False, repr=False)
    _at_cmd_queue: Optional[asyncio.Queue] = field(default=None, init=False, repr=False)
    _on_change: Optional[Callable[[], None]] = field(default=None, init=False, repr=False)

    def _changed_locked(self) -> None:
        self.revision += 1
        self.updated_at = _utc_now()

    def set_connecting(self, address: str) -> int:
        with self._lock:
            self.connection_generation += 1
            generation = self.connection_generation
            self.connected_address = address
            self.connection_state = ConnectionState.CONNECTING
            self._changed_locked()
        self._notify_change()
        return generation

    def set_connected(
        self,
        address: str,
        sock: socket.socket,
        *,
        adapter_address: str | None = None,
        device_name: str | None = None,
    ) -> int:
        with self._lock:
            self.connection_generation += 1
            generation = self.connection_generation
            self.connected_address = address
            self.connected_name = device_name
            if adapter_address is not None:
                self.adapter_address = adapter_address
            self.rfcomm_socket = sock
            self.connection_state = ConnectionState.HANDSHAKING
            self.health["rfcomm"] = "starting"
            self._changed_locked()
        self._notify_change()
        return generation

    def set_handshake_complete(self, generation: int | None = None) -> bool:
        with self._lock:
            if generation is not None and generation != self.connection_generation:
                return False
            self.connection_state = ConnectionState.CONNECTED
            self.health["bluez"] = "ok"
            self.health["rfcomm"] = "ok"
            self._changed_locked()
        self._notify_change()
        return True

    def set_disconnected(self, generation: int | None = None) -> bool:
        """Reset connection-scoped state, ignoring teardown from stale owners."""
        with self._lock:
            if generation is not None and generation != self.connection_generation:
                return False
            self.connection_state = ConnectionState.DISCONNECTED
            self.connected_address = None
            self.connected_name = None
            self.rfcomm_socket = None
            self.call_state = CallState.IDLE
            self.call_direction = CallDirection.UNKNOWN
            self.call_id = None
            self.remote_number = None
            self.remote_number_source = None
            self.remote_name = None
            self.caller_role = "unknown"
            self.call_started_at = None
            self.call_started_at_utc = None
            self.audio_active = False
            self.sco_connected = False
            self.sco_state = SCOState.DISCONNECTED
            self.bridge_state = "stopped"
            self.audio_owner = None
            self.audio_stream_id = None
            self.audio_client_attached = False
            self.audio_queue_ms = 0.0
            self.audio_dropped_frames = 0
            self.audio_queue_peak_ms = 0.0
            self.audio_overflow_events = 0
            self.audio_rejected_bytes = 0
            self.audio_accepted_bytes = 0
            self.audio_consumed_bytes = 0
            self.audio_playback_controlled = False
            self.audio_playback_target_ms = 0.0
            self.audio_playback_underrun_events = 0
            self.audio_playback_underrun_ms = 0.0
            self.audio_capture_queue_ms = 0.0
            self.audio_capture_queue_peak_ms = 0.0
            self.audio_capture_overflow_bytes = 0
            self.indicators = {}
            self.health["rfcomm"] = "stopped"
            self.health["sco"] = "stopped"
            self._changed_locked()
        self._notify_change()
        return True

    def set_call_state(
        self,
        state: CallState,
        *,
        direction: CallDirection | str | None = None,
    ) -> None:
        with self._lock:
            previous = self.call_state
            if direction is not None:
                self.call_direction = CallDirection(direction)
            elif state == CallState.INCOMING:
                self.call_direction = CallDirection.INCOMING
            elif state in (CallState.DIALING, CallState.RINGING):
                self.call_direction = CallDirection.OUTGOING

            if (
                previous == CallState.IDLE
                and state != CallState.IDLE
                and self.remote_number_source == "dialed_pending"
            ):
                if self.call_direction == CallDirection.OUTGOING:
                    self.remote_number_source = "dialed"
                else:
                    # An incoming call raced an outstanding ATD. Never attach
                    # the outbound target (or its privileges) to that caller.
                    self.remote_number = None
                    self.remote_number_source = None
                    self.remote_name = None
                    self.caller_role = "unknown"

            if previous == CallState.IDLE and state != CallState.IDLE:
                self.call_generation += 1
            if state != CallState.IDLE and self.call_id is None:
                self.call_id = uuid.uuid4().hex
            self.call_state = state
            if state == CallState.ACTIVE and self.call_started_at is None:
                self.call_started_at = time.monotonic()
                self.call_started_at_utc = _utc_now()
            elif state == CallState.IDLE:
                self.call_started_at = None
                self.call_started_at_utc = None
                self.call_id = None
                self.call_direction = CallDirection.UNKNOWN
                self.remote_number = None
                self.remote_number_source = None
                self.remote_name = None
                self.caller_role = "unknown"
                self.audio_active = False
                self.audio_client_attached = False
            if state != previous:
                self._changed_locked()
            else:
                return
        self._notify_change()

    def transition_call_state(
        self,
        state: CallState,
        *,
        expected_call_id: str | None,
        expected_generation: int,
        allowed_states: set[CallState] | frozenset[CallState] | tuple[CallState, ...],
        direction: CallDirection | str | None = None,
        expected_connection_generation: int | None = None,
    ) -> bool:
        """Atomically update only the exact call observed before an await."""

        with self._lock:
            if (
                (
                    expected_connection_generation is not None
                    and self.connection_generation
                    != expected_connection_generation
                )
                or self.call_id != expected_call_id
                or self.call_generation != expected_generation
                or self.call_state not in allowed_states
            ):
                return False
            previous = self.call_state
            if direction is not None:
                self.call_direction = CallDirection(direction)
            elif state == CallState.INCOMING:
                self.call_direction = CallDirection.INCOMING
            elif state in (CallState.DIALING, CallState.RINGING):
                self.call_direction = CallDirection.OUTGOING

            if (
                previous == CallState.IDLE
                and state != CallState.IDLE
                and self.remote_number_source == "dialed_pending"
            ):
                if self.call_direction == CallDirection.OUTGOING:
                    self.remote_number_source = "dialed"
                else:
                    self.remote_number = None
                    self.remote_number_source = None
                    self.remote_name = None
                    self.caller_role = "unknown"

            if previous == CallState.IDLE and state != CallState.IDLE:
                self.call_generation += 1
            if state != CallState.IDLE and self.call_id is None:
                self.call_id = uuid.uuid4().hex
            self.call_state = state
            if state == CallState.ACTIVE and self.call_started_at is None:
                self.call_started_at = time.monotonic()
                self.call_started_at_utc = _utc_now()
            elif state == CallState.IDLE:
                self.call_started_at = None
                self.call_started_at_utc = None
                self.call_id = None
                self.call_direction = CallDirection.UNKNOWN
                self.remote_number = None
                self.remote_number_source = None
                self.remote_name = None
                self.caller_role = "unknown"
                self.audio_active = False
                self.audio_client_attached = False
            if state != previous:
                self._changed_locked()
            else:
                return True
        self._notify_change()
        return True

    def set_remote_identity_if(
        self,
        call_id: str,
        call_generation: int,
        number: str | None,
        *,
        source: str,
        name: str | None = None,
        direction: CallDirection | str | None = None,
        caller_role: str | None = None,
    ) -> bool:
        """Set remote identity only if the referenced call is still current."""

        with self._lock:
            if self.call_id != call_id or self.call_generation != call_generation:
                return False
            self.remote_number = number
            self.remote_number_source = source
            self.remote_name = name
            if direction is not None:
                self.call_direction = CallDirection(direction)
            if caller_role is not None:
                self.caller_role = str(caller_role)
            elif number is None:
                self.caller_role = "unknown"
            self._changed_locked()
        self._notify_change()
        return True

    def set_remote_identity(
        self,
        number: str | None,
        *,
        source: str,
        name: str | None = None,
        direction: CallDirection | str | None = None,
        caller_role: str | None = None,
    ) -> None:
        with self._lock:
            self.remote_number = number
            self.remote_number_source = source
            self.remote_name = name
            if direction is not None:
                self.call_direction = CallDirection(direction)
            if caller_role is not None:
                self.caller_role = str(caller_role)
            elif number is None:
                self.caller_role = "unknown"
            self._changed_locked()
        self._notify_change()

    def set_pending_outbound_identity(
        self,
        number: str,
        *,
        caller_role: str,
        expected_connection_generation: int,
        expected_call_generation: int,
    ) -> bool:
        """Bind an ATD target before awaiting its delayed terminal response.

        The compare-and-set guard prevents a stale dial coroutine from
        overwriting a replacement connection or a call that began meanwhile.
        ``dialed_pending`` is deliberately not a verified active-call source;
        the first outgoing call transition promotes it to ``dialed``.
        """

        with self._lock:
            if (
                self.connection_generation != expected_connection_generation
                or self.call_generation != expected_call_generation
                or self.call_state != CallState.IDLE
                or self.call_id is not None
            ):
                return False
            self.remote_number = number
            self.remote_number_source = "dialed_pending"
            self.remote_name = None
            self.call_direction = CallDirection.OUTGOING
            self.caller_role = str(caller_role)
            self._changed_locked()
        self._notify_change()
        return True

    def clear_pending_outbound_identity(
        self,
        number: str,
        *,
        expected_connection_generation: int,
        expected_call_generation: int,
    ) -> bool:
        """Clear only the exact unconfirmed ATD intent observed by a caller."""

        with self._lock:
            if (
                self.connection_generation != expected_connection_generation
                or self.call_generation != expected_call_generation
                or self.call_state != CallState.IDLE
                or self.call_id is not None
                or self.remote_number != number
                or self.remote_number_source != "dialed_pending"
            ):
                return False
            self.remote_number = None
            self.remote_number_source = None
            self.remote_name = None
            self.call_direction = CallDirection.UNKNOWN
            self.caller_role = "unknown"
            self._changed_locked()
        self._notify_change()
        return True

    def set_audio_active(self, active: bool) -> None:
        with self._lock:
            if self.audio_active == active:
                return
            self.audio_active = active
            self._changed_locked()
        self._notify_change()

    def set_sco_connected(self, connected: bool) -> None:
        self.set_sco_state(SCOState.READY if connected else SCOState.DISCONNECTED)

    def set_sco_state(
        self,
        state: SCOState | str,
        *,
        bridge_state: str | None = None,
        owner: str | None = None,
        stream_id: str | None = None,
    ) -> None:
        new_state = SCOState(state)
        with self._lock:
            self.sco_state = new_state
            self.sco_connected = new_state == SCOState.READY
            self.bridge_state = bridge_state or (
                "ready" if self.sco_connected else "stopped"
            )
            if self.sco_connected:
                if owner is not None:
                    self.audio_owner = owner
                if stream_id is not None:
                    if stream_id != self.audio_stream_id:
                        self.audio_client_attached = False
                        self.audio_queue_ms = 0.0
                        self.audio_dropped_frames = 0
                        self.audio_queue_peak_ms = 0.0
                        self.audio_overflow_events = 0
                        self.audio_rejected_bytes = 0
                        self.audio_accepted_bytes = 0
                        self.audio_consumed_bytes = 0
                        self.audio_playback_controlled = False
                        self.audio_playback_target_ms = 0.0
                        self.audio_playback_underrun_events = 0
                        self.audio_playback_underrun_ms = 0.0
                        self.audio_capture_queue_ms = 0.0
                        self.audio_capture_queue_peak_ms = 0.0
                        self.audio_capture_overflow_bytes = 0
                    self.audio_stream_id = stream_id
            else:
                self.audio_owner = None
                self.audio_stream_id = None
                self.audio_client_attached = False
            self.health["sco"] = "ok" if self.sco_connected else self.bridge_state
            self._changed_locked()
        self._notify_change()

    def set_audio_client_attached(
        self,
        attached: bool,
        *,
        stream_id: str | None = None,
    ) -> bool:
        """Record whether the current media lease has an attached client."""

        with self._lock:
            if stream_id is not None and stream_id != self.audio_stream_id:
                return False
            value = bool(attached)
            if self.audio_client_attached == value:
                return True
            self.audio_client_attached = value
            self._changed_locked()
        self._notify_change()
        return True

    def set_audio_metrics(
        self,
        *,
        queue_ms: float,
        dropped_frames: int,
        queue_peak_ms: float = 0.0,
        overflow_events: int = 0,
        rejected_bytes: int = 0,
        accepted_bytes: int = 0,
        consumed_bytes: int = 0,
        playback_controlled: bool = False,
        playback_target_ms: float = 0.0,
        playback_underrun_events: int = 0,
        playback_underrun_ms: float = 0.0,
        capture_queue_ms: float = 0.0,
        capture_queue_peak_ms: float = 0.0,
        capture_overflow_bytes: int = 0,
    ) -> None:
        values = (
            max(0.0, float(queue_ms)),
            max(0, int(dropped_frames)),
            max(0.0, float(queue_peak_ms)),
            max(0, int(overflow_events)),
            max(0, int(rejected_bytes)),
            max(0, int(accepted_bytes)),
            max(0, int(consumed_bytes)),
            bool(playback_controlled),
            max(0.0, float(playback_target_ms)),
            max(0, int(playback_underrun_events)),
            max(0.0, float(playback_underrun_ms)),
            max(0.0, float(capture_queue_ms)),
            max(0.0, float(capture_queue_peak_ms)),
            max(0, int(capture_overflow_bytes)),
        )
        with self._lock:
            current = (
                self.audio_queue_ms,
                self.audio_dropped_frames,
                self.audio_queue_peak_ms,
                self.audio_overflow_events,
                self.audio_rejected_bytes,
                self.audio_accepted_bytes,
                self.audio_consumed_bytes,
                self.audio_playback_controlled,
                self.audio_playback_target_ms,
                self.audio_playback_underrun_events,
                self.audio_playback_underrun_ms,
                self.audio_capture_queue_ms,
                self.audio_capture_queue_peak_ms,
                self.audio_capture_overflow_bytes,
            )
            if current == values:
                return
            (
                self.audio_queue_ms,
                self.audio_dropped_frames,
                self.audio_queue_peak_ms,
                self.audio_overflow_events,
                self.audio_rejected_bytes,
                self.audio_accepted_bytes,
                self.audio_consumed_bytes,
                self.audio_playback_controlled,
                self.audio_playback_target_ms,
                self.audio_playback_underrun_events,
                self.audio_playback_underrun_ms,
                self.audio_capture_queue_ms,
                self.audio_capture_queue_peak_ms,
                self.audio_capture_overflow_bytes,
            ) = values
            self._changed_locked()
        self._notify_change()

    def set_live_ai_state(self, **values: Any) -> None:
        with self._lock:
            if all(self.live_ai.get(key) == value for key, value in values.items()):
                return
            self.live_ai.update(values)
            self._changed_locked()
        self._notify_change()

    def set_health(self, component: str, value: str) -> None:
        with self._lock:
            if self.health.get(component) == value:
                return
            self.health[component] = value
            self._changed_locked()
        self._notify_change()

    def versioned_snapshot(self) -> dict[str, Any]:
        with self._lock:
            duration: Optional[float] = None
            if self.call_started_at is not None:
                duration = round(time.monotonic() - self.call_started_at, 1)
            nested = {
                "schema_version": "hfp.v1",
                "revision": self.revision,
                "updated_at": self.updated_at,
                "server_instance_id": self.server_instance_id,
                "connection": {
                    "state": self.connection_state.value,
                    "generation": self.connection_generation,
                    "adapter_address": self.adapter_address,
                    "device_address": self.connected_address,
                    "device_name": self.connected_name,
                },
                "call": {
                    "id": self.call_id,
                    "generation": self.call_generation,
                    "state": self.call_state.value,
                    "direction": self.call_direction.value,
                    "remote_number": self.remote_number,
                    "remote_number_source": self.remote_number_source,
                    "remote_number_verified": bool(
                        self.remote_number
                        and self.remote_number_source in {"clip", "clcc", "dialed"}
                    ),
                    "remote_name": self.remote_name,
                    "caller_role": self.caller_role,
                    "started_at": self.call_started_at_utc,
                    "duration_seconds": duration,
                },
                "audio": {
                    "codec": "CVSD",
                    "sample_rate_hz": 8000,
                    "sco_state": self.sco_state.value,
                    "bridge_state": self.bridge_state,
                    "owner": self.audio_owner,
                    "stream_id": self.audio_stream_id,
                    "client_attached": self.audio_client_attached,
                    "queue_ms": self.audio_queue_ms,
                    "dropped_frames": self.audio_dropped_frames,
                    "queue_peak_ms": self.audio_queue_peak_ms,
                    "overflow_events": self.audio_overflow_events,
                    "rejected_bytes": self.audio_rejected_bytes,
                    "accepted_bytes": self.audio_accepted_bytes,
                    "consumed_bytes": self.audio_consumed_bytes,
                    "playback_controlled": self.audio_playback_controlled,
                    "playback_target_ms": self.audio_playback_target_ms,
                    "playback_underrun_events": (
                        self.audio_playback_underrun_events
                    ),
                    "playback_underrun_ms": self.audio_playback_underrun_ms,
                    "capture_queue_ms": self.audio_capture_queue_ms,
                    "capture_queue_peak_ms": self.audio_capture_queue_peak_ms,
                    "capture_overflow_bytes": self.audio_capture_overflow_bytes,
                },
                "live_ai": dict(self.live_ai),
                "health": dict(self.health),
            }
            # One-release compatibility surface for existing Hermes clients.
            return nested

    def snapshot(self) -> dict[str, Any]:
        """Return the one-release flat compatibility representation."""
        versioned = self.versioned_snapshot()
        connection = versioned["connection"]
        call = versioned["call"]
        audio = versioned["audio"]
        return {
            "schema_version": versioned["schema_version"],
            "revision": versioned["revision"],
            "updated_at": versioned["updated_at"],
            "server_instance_id": versioned["server_instance_id"],
            "connection": connection["state"],
            "connection_generation": connection["generation"],
            "connected_address": connection["device_address"],
            "connected_name": connection["device_name"],
            "call_state": call["state"],
            "call_id": call["id"],
            "call_generation": call["generation"],
            "call_direction": call["direction"],
            "remote_number": call["remote_number"],
            "remote_number_source": call["remote_number_source"],
            "remote_number_verified": call["remote_number_verified"],
            "remote_name": call["remote_name"],
            "caller_role": call["caller_role"],
            "audio_active": self.audio_active,
            "sco_connected": audio["sco_state"] == SCOState.READY.value,
            "sco_state": audio["sco_state"],
            "audio_owner": audio["owner"],
            "audio_stream_id": audio["stream_id"],
            "audio_client_attached": audio["client_attached"],
            "call_duration_seconds": call["duration_seconds"],
        }

    def _notify_change(self) -> None:
        callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def post_at_event(self, event: Any) -> None:
        if self._asyncio_loop is not None and self._at_event_queue is not None:
            self._asyncio_loop.call_soon_threadsafe(
                self._at_event_queue.put_nowait,
                event,
            )
