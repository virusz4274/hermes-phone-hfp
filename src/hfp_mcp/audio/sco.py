"""Linux SCO audio transport for an HFP call.

The process owns BlueZ's HFP Hands-Free profile, so it also owns the physical
SCO link.  Linux exposes that link as an ``AF_BLUETOOTH`` / ``BTPROTO_SCO``
``SOCK_SEQPACKET`` socket carrying narrow-band, signed 16-bit mono PCM when the
socket's ``BT_VOICE`` setting is ``BT_VOICE_CVSD_16BIT``.

There is exactly one physical SCO transport per :class:`AudioManager`.  Public
``SCOAudioSession`` objects are logical leases on that transport: capture is
fanned out to every running lease and playback from leases is mixed with
saturation.  This keeps the existing session-oriented MCP API while preventing
multiple kernel SCO connections from competing for the same phone.

The transport listens for an Audio-Gateway-initiated SCO connection while also
trying the legacy HF-initiated connection path.  The first valid connection for
the expected phone wins.  A generation number protects a replacement link from
late teardown by an older bridge thread.
"""

from __future__ import annotations

import base64
import logging
import queue
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import (
    AUDIO_BUFFER_MAX_CHUNKS,
    AUDIO_CHANNELS,
    AUDIO_CHUNK_FRAMES,
    AUDIO_SAMPLE_RATE,
    AUDIO_STREAM_FRAME_MS,
)

log = logging.getLogger(__name__)

# Raw bytes per configured chunk: frames x channels x 2 (int16 = 2 bytes/sample).
CHUNK_BYTES: int = AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2
STREAM_FRAME_BYTES: int = (
    AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 * AUDIO_STREAM_FRAME_MS // 1000
)

# Keep capture latency shallow, while allowing the realtime-paced playback
# writer enough jitter tolerance that a brief scheduler/SCO stall does not
# corrupt speech. Exceeding playback capacity fails explicitly; audio is never
# silently overwritten.
CAPTURE_STREAM_MAX_BYTES: int = AUDIO_BUFFER_MAX_CHUNKS * CHUNK_BYTES
PLAYBACK_BUFFER_MAX_MS: int = 2000
PLAYBACK_MAX_BYTES: int = (
    AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 * PLAYBACK_BUFFER_MAX_MS // 1000
)
PLAYBACK_TARGET_MS: int = 160
PLAYBACK_TARGET_BYTES: int = (
    AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 * PLAYBACK_TARGET_MS // 1000
)
MAX_LOGICAL_SESSIONS: int = 8

# Linux Bluetooth socket constants are not exported by Python's socket module
# on all supported Python versions.  Values come from bluetooth/bluetooth.h.
_SOL_BLUETOOTH = 274
_BT_VOICE = 11
_BT_VOICE_CVSD_16BIT = 0x0060

# getsockopt(SOL_SCO, SCO_OPTIONS) -> struct sco_options { uint16 mtu }.
_SOL_SCO = 17
_SCO_OPTIONS = 1

# Bind any local adapter; the controller with the phone's ACL link is selected
# by the kernel.  CPython's BTPROTO_SCO address is the Bluetooth address itself,
# not the tuple forms used by L2CAP and RFCOMM.  ASCII bytes work across the
# supported Python 3.11+ releases.
_BDADDR_ANY = "00:00:00:00:00:00"

# A SCO datagram is normally <= 64 bytes.  SOCK_SEQPACKET still returns one
# complete frame when the supplied receive buffer is larger.
_RECV_BUF = 1024
_SOCKET_POLL_SECONDS = 0.10
_CONNECT_ATTEMPT_SECONDS = 0.50


class SCOAudioError(RuntimeError):
    """Raised when the SCO audio link cannot be established or validated."""


class SCOPlaybackBufferOverflow(SCOAudioError):
    """Raised instead of silently deleting queued caller-facing speech."""


class SCOPlaybackProtocolError(SCOAudioError):
    """Raised for an invalid ordered playback-control sequence."""


@dataclass
class _BufferedPlaybackUtterance:
    """One explicitly delimited caller-facing utterance.

    ``phase`` is one of ``prebuffering``, ``playing``, ``gap_pending``, or
    ``rebuffering``.  A gap remains provisional until later PCM proves that
    speech was still in progress; an ordered end marker instead dismisses it
    as normal post-utterance silence.
    """

    utterance_id: str | None
    pcm: bytearray = field(default_factory=bytearray)
    ended: bool = False
    phase: str = "prebuffering"
    provisional_gap_bytes: int = 0
    provisional_gap_packets: int = 0


@dataclass(frozen=True)
class SCOHealthEvent:
    """Immutable physical-link state delivered to registered callbacks."""

    state: str
    address: str
    generation: int
    reason: Optional[str] = None
    direction: Optional[str] = None
    mtu: Optional[int] = None
    voice_setting: Optional[int] = None

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "connected": self.connected,
            "address": self.address,
            "generation": self.generation,
            "reason": self.reason,
            "direction": self.direction,
            "mtu": self.mtu,
            "voice_setting": self.voice_setting,
        }


SCOHealthCallback = Callable[[SCOHealthEvent], None]


def _canonical_address(address: str) -> str:
    parts = address.split(":")
    if len(parts) != 6 or any(
        len(part) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in part)
        for part in parts
    ):
        raise ValueError(f"Invalid Bluetooth address: {address!r}")
    return ":".join(part.upper() for part in parts)


def _sco_address(address: str) -> bytes:
    return address.encode("ascii")


def _sco_peer_address(peer: object) -> str:
    """Return an accepted SCO peer as text across CPython/OS variants."""

    if isinstance(peer, tuple) and peer:
        # Defensive compatibility for older/non-CPython wrappers.  Native
        # CPython BTPROTO_SCO returns the address directly.
        peer = peer[0]
    if isinstance(peer, bytes):
        try:
            return peer.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid non-ASCII SCO peer address") from exc
    if isinstance(peer, str):
        return peer
    raise ValueError(f"Invalid SCO peer address: {peer!r}")


def _new_sco_socket() -> socket.socket:
    return socket.socket(
        socket.AF_BLUETOOTH,
        socket.SOCK_SEQPACKET,
        socket.BTPROTO_SCO,
    )


def _safe_close(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _unpack_u16(raw: object, option_name: str) -> int:
    if isinstance(raw, int):
        return raw & 0xFFFF
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) < 2:
        raise SCOAudioError(f"Kernel returned an invalid {option_name} value: {raw!r}")
    return struct.unpack("=H", bytes(raw[:2]))[0]


def _read_voice_setting(sock: socket.socket) -> int:
    try:
        raw = sock.getsockopt(_SOL_BLUETOOTH, _BT_VOICE, 2)
    except OSError as exc:
        raise SCOAudioError(f"Could not read SCO BT_VOICE setting: {exc}") from exc
    return _unpack_u16(raw, "BT_VOICE")


def _configure_cvsd(sock: socket.socket) -> int:
    """Set and verify the Linux 16-bit CVSD PCM data path on an SCO socket."""
    packed = struct.pack("=H", _BT_VOICE_CVSD_16BIT)
    try:
        sock.setsockopt(_SOL_BLUETOOTH, _BT_VOICE, packed)
    except OSError as exc:
        raise SCOAudioError(f"Could not configure SCO for 16-bit CVSD PCM: {exc}") from exc
    return _verify_cvsd(sock)


def _verify_cvsd(sock: socket.socket) -> int:
    setting = _read_voice_setting(sock)
    if setting != _BT_VOICE_CVSD_16BIT:
        raise SCOAudioError(
            "SCO socket is not using 16-bit CVSD PCM "
            f"(BT_VOICE=0x{setting:04x}, expected 0x{_BT_VOICE_CVSD_16BIT:04x})"
        )
    return setting


def _read_sco_mtu(sock: socket.socket) -> Optional[int]:
    """Best-effort read of the negotiated SCO MTU."""
    try:
        raw = sock.getsockopt(_SOL_SCO, _SCO_OPTIONS, 2)
        mtu = _unpack_u16(raw, "SCO_OPTIONS")
        return mtu if mtu > 0 else None
    except (OSError, SCOAudioError):
        return None


def _mix_s16le(parts: list[bytes], size: int) -> bytes:
    """Mix zero or more partial s16le frames with saturation to ``size`` bytes."""
    if not parts:
        return b"\x00" * size
    if len(parts) == 1:
        return parts[0] + b"\x00" * (size - len(parts[0]))

    out = bytearray(size)
    sample_bytes = size - (size % 2)
    for offset in range(0, sample_bytes, 2):
        mixed = 0
        for part in parts:
            if offset + 2 <= len(part):
                mixed += int.from_bytes(part[offset:offset + 2], "little", signed=True)
        mixed = max(-32768, min(32767, mixed))
        out[offset:offset + 2] = mixed.to_bytes(2, "little", signed=True)

    # SCO PCM frames should be sample-aligned.  Preserve an odd trailing byte
    # deterministically if a non-conforming producer supplied one.
    if size % 2:
        for part in parts:
            if len(part) == size:
                out[-1] = part[-1]
                break
    return bytes(out)


class _SCOTransport:
    """One physical, generation-protected SCO socket shared by logical leases."""

    def __init__(
        self,
        phone_address: str,
        health_callback: Optional[SCOHealthCallback] = None,
    ) -> None:
        self.address = _canonical_address(phone_address)
        self._health_callback = health_callback
        self._condition = threading.Condition(threading.RLock())
        self._leases: dict[int, SCOAudioSession] = {}

        self._state = "stopped"
        self._reason: Optional[str] = None
        self._direction: Optional[str] = None
        self._generation = 0
        self._voice_setting: Optional[int] = None
        self._mtu: Optional[int] = None
        self._sock: Optional[socket.socket] = None
        self._bridge: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._bridge_gate: Optional[threading.Event] = None
        self._startup_failures: dict[int, str] = {}

    @property
    def socket(self) -> Optional[socket.socket]:
        with self._condition:
            return self._sock

    @property
    def bridge(self) -> Optional[threading.Thread]:
        with self._condition:
            return self._bridge

    @property
    def mtu(self) -> Optional[int]:
        with self._condition:
            return self._mtu

    def is_connected(self) -> bool:
        with self._condition:
            return self._state == "connected" and self._sock is not None

    def has_lease(self, lease: SCOAudioSession) -> bool:
        with self._condition:
            return id(lease) in self._leases

    def health_event(self) -> SCOHealthEvent:
        with self._condition:
            return self._health_event_locked()

    def _health_event_locked(self) -> SCOHealthEvent:
        return SCOHealthEvent(
            state=self._state,
            address=self.address,
            generation=self._generation,
            reason=self._reason,
            direction=self._direction,
            mtu=self._mtu,
            voice_setting=self._voice_setting,
        )

    def _lease_snapshot_locked(self) -> list[SCOAudioSession]:
        return list(self._leases.values())

    def _publish(
        self,
        event: SCOHealthEvent,
        leases: Optional[list[SCOAudioSession]] = None,
        *,
        notify_manager: bool = True,
    ) -> None:
        if leases is None:
            with self._condition:
                leases = self._lease_snapshot_locked()
        for lease in leases:
            lease._on_transport_health(event)
        if notify_manager and self._health_callback is not None:
            try:
                self._health_callback(event)
            except Exception:
                log.exception("SCO health callback failed")

    def acquire(
        self,
        lease: SCOAudioSession,
        timeout: float,
    ) -> tuple[socket.socket, SCOHealthEvent]:
        """Attach a lease, establishing the physical link if necessary."""
        if timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        deadline = time.monotonic() + timeout
        starter = False

        with self._condition:
            self._leases[id(lease)] = lease
            if self._state == "connected" and self._sock is not None:
                event = self._health_event_locked()
                sock = self._sock
                leases = [lease]
                target_generation = self._generation
            elif self._state == "connecting":
                target_generation = self._generation
                event = None
                sock = None
                leases = None
            else:
                self._generation += 1
                target_generation = self._generation
                self._state = "connecting"
                self._reason = None
                self._direction = None
                self._voice_setting = None
                self._mtu = None
                self._stop_event = threading.Event()
                self._bridge_gate = threading.Event()
                event = self._health_event_locked()
                leases = self._lease_snapshot_locked()
                sock = None
                starter = True

        if event is not None and not starter:
            # A new lease joined an already healthy physical link.  This is a
            # lease-local replay, not a physical manager health transition.
            self._publish(event, leases, notify_manager=False)
            return sock, event  # type: ignore[return-value]

        if starter:
            self._publish(event, leases)  # type: ignore[arg-type]
            established: Optional[socket.socket] = None
            try:
                stop_event = self._stop_event
                if stop_event is None:
                    raise SCOAudioError("SCO startup was cancelled")
                established, direction = self._establish_sco(
                    max(0.001, deadline - time.monotonic()), stop_event
                )
                voice_setting = _verify_cvsd(established)
                mtu = _read_sco_mtu(established)
            except Exception as exc:
                _safe_close(established)
                error = exc if isinstance(exc, SCOAudioError) else SCOAudioError(str(exc))
                self._record_start_failure(target_generation, str(error))
                with self._condition:
                    self._leases.pop(id(lease), None)
                raise error from exc

            with self._condition:
                cancelled = (
                    self._generation != target_generation
                    or self._state != "connecting"
                    or self._stop_event is not stop_event
                    or stop_event.is_set()
                )
                if not cancelled:
                    self._sock = established
                    self._direction = direction
                    self._voice_setting = voice_setting
                    self._mtu = mtu
                    self._state = "connected"
                    self._reason = None
                    gate = self._bridge_gate
                    bridge = threading.Thread(
                        target=self._bridge_loop,
                        args=(target_generation, established, stop_event, gate),
                        daemon=True,
                        name=f"sco-physical-{target_generation}",
                    )
                    self._bridge = bridge
                    connected_event = self._health_event_locked()
                    connected_leases = self._lease_snapshot_locked()
                    self._condition.notify_all()

            if cancelled:
                _safe_close(established)
                with self._condition:
                    self._leases.pop(id(lease), None)
                raise SCOAudioError("SCO startup was cancelled")

            try:
                bridge.start()
            except RuntimeError as exc:
                self._bridge_ended(
                    target_generation,
                    established,
                    f"Could not start SCO bridge thread: {exc}",
                )
                with self._condition:
                    self._leases.pop(id(lease), None)
                raise SCOAudioError(str(exc)) from exc

            # Publish readiness before allowing the bridge to report terminal
            # health.  Otherwise an already-closed peer can race ``failed``
            # ahead of this ``connected`` event and leave observers healthy.
            try:
                self._publish(connected_event, connected_leases)
            finally:
                if gate is not None:
                    gate.set()
            with self._condition:
                still_acquired = (
                    self._generation == target_generation
                    and self._state == "connected"
                    and self._sock is established
                    and id(lease) in self._leases
                )
            if not still_acquired:
                raise SCOAudioError("SCO lease was released during startup")
            log.info(
                "SCO transport up (peer=%s, direction=%s, mtu=%s, generation=%d)",
                self.address,
                direction,
                mtu,
                target_generation,
            )
            return established, connected_event

        # Another lease is establishing this generation.  It receives exactly
        # the same socket or the same startup failure, never starts a second one.
        with self._condition:
            while True:
                failure = self._startup_failures.get(target_generation)
                if failure is not None:
                    self._leases.pop(id(lease), None)
                    raise SCOAudioError(failure)
                if (
                    self._generation == target_generation
                    and self._state == "connected"
                    and self._sock is not None
                    and id(lease) in self._leases
                ):
                    shared_socket = self._sock
                    shared_event = self._health_event_locked()
                    break
                if id(lease) not in self._leases:
                    raise SCOAudioError("SCO lease was released during startup")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._leases.pop(id(lease), None)
                    raise SCOAudioError("Timed out waiting for shared SCO transport")
                self._condition.wait(remaining)
        self._publish(shared_event, [lease], notify_manager=False)
        return shared_socket, shared_event

    def _record_start_failure(self, generation: int, reason: str) -> None:
        with self._condition:
            if self._generation != generation:
                return
            self._state = "failed"
            self._reason = reason
            self._sock = None
            self._bridge = None
            self._startup_failures[generation] = reason
            # Keep only a few generations for concurrent waiters.
            for old in sorted(self._startup_failures)[:-4]:
                self._startup_failures.pop(old, None)
            event = self._health_event_locked()
            leases = self._lease_snapshot_locked()
            self._condition.notify_all()
        self._publish(event, leases)

    def release(self, lease: SCOAudioSession) -> None:
        """Release one logical lease and stop the link after the final lease."""
        with self._condition:
            if self._leases.pop(id(lease), None) is None:
                return
            if self._leases:
                return

            stop_event = self._stop_event
            gate = self._bridge_gate
            sock = self._sock
            bridge = self._bridge
            was_live = self._state != "stopped"
            if stop_event is not None:
                stop_event.set()
            if gate is not None:
                gate.set()
            self._sock = None
            self._bridge = None
            self._state = "stopped"
            self._reason = None
            self._direction = None
            self._voice_setting = None
            self._mtu = None
            event = self._health_event_locked()
            self._condition.notify_all()

        _safe_close(sock)
        if bridge is not None and bridge is not threading.current_thread() and bridge.is_alive():
            bridge.join(timeout=2.0)
        if bridge is not None and bridge.is_alive():
            log.warning("SCO bridge generation %d did not stop within 2s", event.generation)
        if was_live:
            # No leases remain, but the manager still needs the physical state.
            self._publish(event, [], notify_manager=True)
            log.info("SCO transport stopped (peer=%s)", self.address)

    # ------------------------------------------------------------------
    # Connection establishment
    # ------------------------------------------------------------------

    def _open_listener(self) -> socket.socket:
        listener = _new_sco_socket()
        try:
            listener.bind(_sco_address(_BDADDR_ANY))
            _configure_cvsd(listener)
            listener.listen(1)
            listener.settimeout(_SOCKET_POLL_SECONDS)
            return listener
        except Exception:
            _safe_close(listener)
            raise

    def _connect_sco(
        self,
        timeout: float,
        stop_event: Optional[threading.Event] = None,
        race_cancel: Optional[threading.Event] = None,
    ) -> socket.socket:
        """Retry HF-initiated SCO setup until success, cancellation, or timeout."""
        deadline = time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        while time.monotonic() < deadline:
            if (stop_event is not None and stop_event.is_set()) or (
                race_cancel is not None and race_cancel.is_set()
            ):
                raise SCOAudioError("SCO connection attempt cancelled")

            sock = _new_sco_socket()
            try:
                sock.bind(_sco_address(_BDADDR_ANY))
                _configure_cvsd(sock)
                remaining = deadline - time.monotonic()
                sock.settimeout(max(0.01, min(_CONNECT_ATTEMPT_SECONDS, remaining)))
                sock.connect(_sco_address(self.address))
                _verify_cvsd(sock)
                if (stop_event is not None and stop_event.is_set()) or (
                    race_cancel is not None and race_cancel.is_set()
                ):
                    raise SCOAudioError("SCO connection attempt cancelled")
                sock.settimeout(None)
                return sock
            except SCOAudioError:
                _safe_close(sock)
                raise
            except OSError as exc:
                last_exc = exc
                _safe_close(sock)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if stop_event is not None and stop_event.wait(min(0.05, remaining)):
                    break
                if race_cancel is not None and race_cancel.is_set():
                    break

        if (stop_event is not None and stop_event.is_set()) or (
            race_cancel is not None and race_cancel.is_set()
        ):
            raise SCOAudioError("SCO connection attempt cancelled")
        raise SCOAudioError(
            f"Could not establish outbound SCO link to {self.address}: {last_exc}"
        )

    def _establish_sco(
        self,
        timeout: float,
        stop_event: threading.Event,
    ) -> tuple[socket.socket, str]:
        """Race inbound accept against outbound connect; return the valid winner."""
        deadline = time.monotonic() + timeout
        listener: Optional[socket.socket] = None
        try:
            listener = self._open_listener()
        except (OSError, SCOAudioError) as exc:
            # Some controllers/kernels cannot bind a listener concurrently with
            # their existing audio setup.  Outbound remains a supported fallback.
            log.debug("SCO inbound listener unavailable: %s", exc)
            return self._connect_sco(timeout, stop_event), "outbound"

        results: queue.Queue[tuple[Optional[socket.socket], Optional[Exception]]] = (
            queue.Queue(maxsize=1)
        )
        race_cancel = threading.Event()

        def _outbound() -> None:
            try:
                connected = self._connect_sco(
                    max(0.001, deadline - time.monotonic()),
                    stop_event,
                    race_cancel,
                )
            except Exception as exc:
                try:
                    results.put_nowait((None, exc))
                except queue.Full:
                    pass
                return
            if race_cancel.is_set() or stop_event.is_set():
                _safe_close(connected)
                return
            try:
                results.put_nowait((connected, None))
            except queue.Full:
                _safe_close(connected)

        outbound = threading.Thread(
            target=_outbound,
            daemon=True,
            name=f"sco-connect-{self.address}",
        )
        outbound.start()
        winner: Optional[socket.socket] = None
        outbound_error: Optional[Exception] = None
        listener_error: Optional[Exception] = None
        try:
            while time.monotonic() < deadline and not stop_event.is_set():
                try:
                    connected, error = results.get_nowait()
                except queue.Empty:
                    connected = None
                    error = None
                if connected is not None:
                    winner = connected
                    return winner, "outbound"
                if error is not None:
                    outbound_error = error

                try:
                    accepted, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    listener_error = exc
                    _safe_close(listener)
                    listener = None
                    break

                try:
                    peer_address = _sco_peer_address(peer)
                    expected_peer = _canonical_address(peer_address) == self.address
                except ValueError:
                    peer_address = repr(peer)
                    expected_peer = False
                if not expected_peer:
                    log.warning(
                        "Rejected SCO link from unexpected peer %s (expected %s)",
                        peer_address,
                        self.address,
                    )
                    _safe_close(accepted)
                    continue
                try:
                    _verify_cvsd(accepted)
                    accepted.settimeout(None)
                except (OSError, SCOAudioError) as exc:
                    listener_error = exc
                    _safe_close(accepted)
                    continue
                winner = accepted
                return winner, "inbound"

            # The listener can fail while the outbound connector is still in a
            # short kernel connect attempt.  Give it only the remaining budget.
            while time.monotonic() < deadline and not stop_event.is_set():
                try:
                    connected, error = results.get(
                        timeout=min(_SOCKET_POLL_SECONDS, deadline - time.monotonic())
                    )
                except queue.Empty:
                    continue
                if connected is not None:
                    winner = connected
                    return winner, "outbound"
                if error is not None:
                    outbound_error = error
                    break
        finally:
            race_cancel.set()
            _safe_close(listener)
            outbound.join(timeout=_CONNECT_ATTEMPT_SECONDS + 0.1)
            # A late outbound success must not survive an inbound winner.
            try:
                late_socket, _late_error = results.get_nowait()
            except queue.Empty:
                late_socket = None
            if late_socket is not None and late_socket is not winner:
                _safe_close(late_socket)

        if stop_event.is_set():
            raise SCOAudioError("SCO connection attempt cancelled")
        detail = outbound_error or listener_error or "timed out"
        raise SCOAudioError(f"Could not establish SCO audio link to {self.address}: {detail}")

    # ------------------------------------------------------------------
    # Duplex bridge
    # ------------------------------------------------------------------

    def _bridge_loop(
        self,
        generation: int,
        sock: socket.socket,
        stop_event: threading.Event,
        gate: Optional[threading.Event],
    ) -> None:
        if gate is not None:
            gate.wait()
        failure: Optional[str] = None
        while not stop_event.is_set():
            try:
                frame = sock.recv(_RECV_BUF)
            except OSError as exc:
                if not stop_event.is_set():
                    failure = f"SCO receive failed: {exc}"
                break
            if not frame:
                if not stop_event.is_set():
                    failure = "Peer closed the SCO audio link"
                break

            with self._condition:
                if generation != self._generation or self._sock is not sock:
                    break
                leases = self._lease_snapshot_locked()
            for lease in leases:
                if lease.running:
                    lease._absorb_capture(frame)

            playback = [lease._take_playback_raw(len(frame)) for lease in leases if lease.running]
            out = _mix_s16le([part for part in playback if part], len(frame))
            try:
                sent = sock.send(out)
                if sent != len(out):
                    failure = f"SCO short send ({sent} of {len(out)} bytes)"
                    break
            except OSError as exc:
                if not stop_event.is_set():
                    failure = f"SCO send failed: {exc}"
                break

        self._bridge_ended(generation, sock, failure)

    def _bridge_ended(
        self,
        generation: int,
        sock: socket.socket,
        failure: Optional[str],
    ) -> None:
        _safe_close(sock)
        with self._condition:
            # A stale bridge is never allowed to clear a replacement socket.
            if generation != self._generation or self._sock is not sock:
                return
            self._sock = None
            self._bridge = None
            if self._stop_event is not None:
                self._stop_event.set()
            if failure is None:
                self._state = "stopped"
                self._reason = None
            else:
                self._state = "failed"
                self._reason = failure
            event = self._health_event_locked()
            leases = self._lease_snapshot_locked()
            self._condition.notify_all()
        self._publish(event, leases)
        if failure is not None:
            log.warning(
                "SCO transport failed (peer=%s, generation=%d): %s",
                self.address,
                generation,
                failure,
            )


class SCOAudioSession:
    """A logical capture/playback lease on one physical SCO transport."""

    def __init__(
        self,
        session_id: str,
        phone_address: str,
        *,
        health_callback: Optional[SCOHealthCallback] = None,
        _transport: Optional[_SCOTransport] = None,
    ) -> None:
        self.session_id = session_id
        self._address = _canonical_address(phone_address)
        self._transport = _transport or _SCOTransport(self._address)
        self.mtu: Optional[int] = None

        self._capture_buf: deque[bytes] = deque(maxlen=AUDIO_BUFFER_MAX_CHUNKS)
        self._capture_accum = bytearray()
        self._capture_lock = threading.Lock()
        self._stream_capture = bytearray()
        self._stream_lock = threading.Lock()
        self._capture_received_bytes = 0
        self._capture_consumed_bytes = 0
        self._capture_overflow_bytes = 0
        self._capture_overflow_events = 0
        self._capture_queue_peak_bytes = 0
        self._capture_teardown_discarded_bytes = 0
        self._playback = bytearray()
        self._buffered_playback: deque[_BufferedPlaybackUtterance] = deque()
        self._buffered_playback_bytes = 0
        self._playback_controlled = False
        self._pb_lock = threading.Lock()
        self._playback_accepted_bytes = 0
        self._playback_consumed_bytes = 0
        self._playback_cleared_bytes = 0
        self._playback_teardown_discarded_bytes = 0
        self._playback_rejected_bytes = 0
        self._playback_overflow_bytes = 0
        self._playback_overflow_events = 0
        self._playback_queue_peak_bytes = 0
        self._playback_utterances_started = 0
        self._playback_utterances_completed = 0
        self._playback_utterances_aborted = 0
        self._playback_short_utterances = 0
        self._playback_prebuffer_silence_bytes = 0
        self._playback_prebuffer_silence_packets = 0
        self._playback_rebuffer_completions = 0
        self._playback_underrun_events = 0
        self._playback_underrun_silence_bytes = 0
        self._playback_underrun_silence_packets = 0
        self._playback_late_arrival_bytes = 0
        self._playback_provisional_gap_events = 0
        self._playback_provisional_gap_dismissed_bytes = 0
        self._playback_provisional_gap_cancelled_bytes = 0

        self._lifecycle_lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._callbacks: list[SCOHealthCallback] = []
        if health_callback is not None:
            self._callbacks.append(health_callback)
        self._health = SCOHealthEvent("stopped", self._address, 0)
        self._running = False
        self._accept_transport_events = False

        # Compatibility attributes retained for callers which inspect them.
        # All leases point at the same socket/thread while running.
        self._sock: Optional[socket.socket] = None
        self._bridge: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        with self._health_lock:
            return self._running

    def health(self) -> dict[str, object]:
        with self._health_lock:
            event = self._health
            running = self._running
        return {**event.as_dict(), "session_id": self.session_id, "running": running}

    def add_health_callback(
        self,
        callback: SCOHealthCallback,
        *,
        replay: bool = True,
    ) -> None:
        with self._health_lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
            event = self._health
        if replay:
            try:
                callback(event)
            except Exception:
                log.exception("SCO session health callback failed")

    def remove_health_callback(self, callback: SCOHealthCallback) -> None:
        with self._health_lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def _on_transport_health(self, event: SCOHealthEvent) -> None:
        with self._health_lock:
            if not self._accept_transport_events:
                return
            self._health = event
            clear_buffers = False
            if event.connected:
                self._running = True
                self.mtu = event.mtu
                self._sock = self._transport.socket
                self._bridge = self._transport.bridge
            elif event.state in ("failed", "stopped"):
                self._running = False
                self._sock = None
                self._bridge = None
                clear_buffers = True
            callbacks = list(self._callbacks)
        if clear_buffers:
            # Never replay audio queued for an older physical generation.
            self._clear_buffers()
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                log.exception("SCO session health callback failed")

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self, connect_timeout: float = 5.0) -> None:
        """Acquire the shared SCO link and make this logical lease active."""
        with self._lifecycle_lock:
            if self.running and self._transport.is_connected():
                return
            self._stop.clear()
            with self._health_lock:
                self._accept_transport_events = True
            try:
                sock, event = self._transport.acquire(self, connect_timeout)
            except Exception:
                with self._health_lock:
                    self._accept_transport_events = False
                    self._running = False
                    self._sock = None
                    self._bridge = None
                raise
            with self._health_lock:
                self._health = event
                self._running = True
                self._sock = sock
                self._bridge = self._transport.bridge
                self.mtu = event.mtu
            log.info(
                "SCO audio lease %s active (peer=%s, mtu=%s, generation=%d)",
                self.session_id,
                self._address,
                self.mtu,
                event.generation,
            )

    def stop(self) -> None:
        """Release this lease; the final lease closes the physical SCO link."""
        with self._lifecycle_lock:
            with self._health_lock:
                self._accept_transport_events = False
            self._transport.release(self)
            self._stop.set()
            event = self._transport.health_event()
            stopped = SCOHealthEvent(
                state="stopped",
                address=self._address,
                generation=event.generation,
                reason=None,
            )
            with self._health_lock:
                self._health = stopped
                self._running = False
                self._sock = None
                self._bridge = None
                self.mtu = None
                callbacks = list(self._callbacks)
            self._clear_buffers()
        for callback in callbacks:
            try:
                callback(stopped)
            except Exception:
                log.exception("SCO session health callback failed")
        log.info("SCO audio lease %s stopped", self.session_id)

    def _clear_buffers(self) -> None:
        with self._capture_lock:
            self._capture_buf.clear()
            self._capture_accum.clear()
        with self._stream_lock:
            self._capture_teardown_discarded_bytes += len(self._stream_capture)
            self._stream_capture.clear()
        with self._pb_lock:
            discarded = self._playback_queued_bytes_locked()
            self._playback_teardown_discarded_bytes += discarded
            self._playback.clear()
            self._cancel_buffered_playback_locked(teardown=True)
            self._playback_controlled = False

    # Retain the former private helper for diagnostic callers.  The manager's
    # physical transport normally calls it as part of the accept/connect race.
    def _connect_sco(self, timeout: float) -> socket.socket:
        return self._transport._connect_sco(timeout)

    # ------------------------------------------------------------------
    # Capture and playback
    # ------------------------------------------------------------------

    def _absorb_capture(self, frame: bytes) -> None:
        """Fan-in a raw SCO frame to this lease's bounded capture buffers."""
        with self._capture_lock:
            self._capture_accum.extend(frame)
            while len(self._capture_accum) >= CHUNK_BYTES:
                chunk = bytes(self._capture_accum[:CHUNK_BYTES])
                del self._capture_accum[:CHUNK_BYTES]
                self._capture_buf.append(chunk)
        with self._stream_lock:
            self._capture_received_bytes += len(frame)
            self._stream_capture.extend(frame)
            overflow = len(self._stream_capture) - CAPTURE_STREAM_MAX_BYTES
            if overflow > 0:
                del self._stream_capture[:overflow]
                self._capture_overflow_bytes += overflow
                self._capture_overflow_events += 1
            self._capture_queue_peak_bytes = max(
                self._capture_queue_peak_bytes,
                len(self._stream_capture),
            )

    def get_chunk(self) -> Optional[bytes]:
        """Pop the oldest configured PCM chunk, or None when the buffer is empty."""
        with self._capture_lock:
            try:
                return self._capture_buf.popleft()
            except IndexError:
                return None

    def get_chunk_b64(self) -> Optional[str]:
        chunk = self.get_chunk()
        return base64.b64encode(chunk).decode("ascii") if chunk else None

    def pop_stream_frame(self, n: int = STREAM_FRAME_BYTES) -> Optional[bytes]:
        """Pop one low-latency PCM frame for realtime sidecar streaming."""
        if n <= 0:
            raise ValueError("stream frame size must be positive")
        with self._stream_lock:
            if len(self._stream_capture) < n:
                return None
            frame = bytes(self._stream_capture[:n])
            del self._stream_capture[:n]
            self._capture_consumed_bytes += len(frame)
            return frame

    def _take_playback_raw(self, n: int) -> bytes:
        with self._pb_lock:
            if self._playback_controlled:
                return self._take_buffered_playback_locked(n)
            take = bytes(self._playback[:n])
            del self._playback[:len(take)]
            self._playback_consumed_bytes += len(take)
            return take

    def _take_playback(self, n: int) -> bytes:
        """Pop playback bytes and zero-pad to exactly ``n`` bytes."""
        take = self._take_playback_raw(n)
        return take + b"\x00" * (n - len(take))

    def queue_playback(self, pcm_bytes: bytes) -> None:
        """Queue raw 8 kHz s16le mono PCM for the remote party."""
        if not pcm_bytes:
            return
        if not self.running or not self._transport.is_connected():
            raise RuntimeError("SCO audio session is not running")
        with self._pb_lock:
            required = self._playback_queued_bytes_locked() + len(pcm_bytes)
            if required > PLAYBACK_MAX_BYTES:
                self._playback_overflow_events += 1
                self._playback_overflow_bytes += required - PLAYBACK_MAX_BYTES
                self._playback_rejected_bytes += len(pcm_bytes)
                raise SCOPlaybackBufferOverflow(
                    "SCO playback buffer full; refusing to overwrite queued speech"
                )
            if self._playback_controlled:
                utterance = self._open_playback_utterance_locked()
                if utterance.phase == "gap_pending":
                    self._confirm_provisional_gap_locked(utterance)
                if utterance.phase == "rebuffering":
                    self._playback_late_arrival_bytes += len(pcm_bytes)
                utterance.pcm.extend(pcm_bytes)
                self._buffered_playback_bytes += len(pcm_bytes)
            else:
                self._playback.extend(pcm_bytes)
            self._playback_accepted_bytes += len(pcm_bytes)
            self._playback_queue_peak_bytes = max(
                self._playback_queue_peak_bytes,
                required,
            )

    def start_buffered_playback(self, utterance_id: str | None = None) -> None:
        """Open an ordered, jitter-buffered playback utterance.

        This mode is opt-in through the sidecar control protocol.  Binary-only
        legacy clients therefore retain the former immediate-playback behavior.
        """

        if not self.running or not self._transport.is_connected():
            raise RuntimeError("SCO audio session is not running")
        with self._pb_lock:
            if self._playback:
                raise SCOPlaybackProtocolError(
                    "cannot enable controlled playback with legacy PCM queued"
                )
            if self._buffered_playback and not self._buffered_playback[-1].ended:
                raise SCOPlaybackProtocolError("playback utterance already open")
            self._playback_controlled = True
            self._buffered_playback.append(
                _BufferedPlaybackUtterance(utterance_id=utterance_id)
            )
            self._playback_utterances_started += 1

    def end_buffered_playback(self, utterance_id: str | None = None) -> None:
        """Close the latest ordered playback utterance without dropping PCM."""

        if not self.running or not self._transport.is_connected():
            raise RuntimeError("SCO audio session is not running")
        with self._pb_lock:
            utterance = self._open_playback_utterance_locked()
            if (
                utterance_id is not None
                and utterance.utterance_id is not None
                and utterance_id != utterance.utterance_id
            ):
                raise SCOPlaybackProtocolError("playback utterance id mismatch")
            utterance.ended = True
            if utterance.phase == "gap_pending":
                self._dismiss_provisional_gap_locked(utterance)
                utterance.phase = "playing"

    def _open_playback_utterance_locked(self) -> _BufferedPlaybackUtterance:
        if not self._playback_controlled or not self._buffered_playback:
            raise SCOPlaybackProtocolError("playback_start is required before PCM")
        utterance = self._buffered_playback[-1]
        if utterance.ended:
            raise SCOPlaybackProtocolError("playback_start is required after playback_end")
        return utterance

    def _playback_queued_bytes_locked(self) -> int:
        return len(self._playback) + self._buffered_playback_bytes

    def _confirm_provisional_gap_locked(
        self, utterance: _BufferedPlaybackUtterance
    ) -> None:
        self._playback_underrun_events += 1
        self._playback_underrun_silence_bytes += utterance.provisional_gap_bytes
        self._playback_underrun_silence_packets += utterance.provisional_gap_packets
        utterance.provisional_gap_bytes = 0
        utterance.provisional_gap_packets = 0
        utterance.phase = "rebuffering"

    def _dismiss_provisional_gap_locked(
        self, utterance: _BufferedPlaybackUtterance
    ) -> None:
        self._playback_provisional_gap_dismissed_bytes += (
            utterance.provisional_gap_bytes
        )
        utterance.provisional_gap_bytes = 0
        utterance.provisional_gap_packets = 0

    def _complete_head_utterance_locked(self) -> None:
        utterance = self._buffered_playback.popleft()
        if utterance.provisional_gap_bytes:
            self._dismiss_provisional_gap_locked(utterance)
        self._playback_utterances_completed += 1

    def _take_buffered_playback_locked(self, n: int) -> bytes:
        if n <= 0:
            return b""
        while self._buffered_playback:
            utterance = self._buffered_playback[0]
            if utterance.ended and not utterance.pcm:
                self._complete_head_utterance_locked()
                continue
            break
        if not self._buffered_playback:
            return b""

        utterance = self._buffered_playback[0]
        if utterance.phase == "prebuffering":
            if len(utterance.pcm) >= PLAYBACK_TARGET_BYTES or utterance.ended:
                if utterance.ended and len(utterance.pcm) < PLAYBACK_TARGET_BYTES:
                    self._playback_short_utterances += 1
                utterance.phase = "playing"
            else:
                self._playback_prebuffer_silence_bytes += n
                self._playback_prebuffer_silence_packets += 1
                return b""
        elif utterance.phase == "rebuffering":
            if len(utterance.pcm) >= PLAYBACK_TARGET_BYTES or utterance.ended:
                utterance.phase = "playing"
                self._playback_rebuffer_completions += 1
            else:
                self._playback_underrun_silence_bytes += n
                self._playback_underrun_silence_packets += 1
                return b""
        elif utterance.phase == "gap_pending":
            utterance.provisional_gap_bytes += n
            utterance.provisional_gap_packets += 1
            return b""

        if len(utterance.pcm) < n and not utterance.ended:
            utterance.phase = "gap_pending"
            utterance.provisional_gap_bytes += n
            utterance.provisional_gap_packets += 1
            self._playback_provisional_gap_events += 1
            return b""

        take = bytes(utterance.pcm[:n])
        del utterance.pcm[:len(take)]
        self._buffered_playback_bytes -= len(take)
        self._playback_consumed_bytes += len(take)
        if utterance.ended and not utterance.pcm:
            self._complete_head_utterance_locked()
        return take

    def _cancel_buffered_playback_locked(self, *, teardown: bool = False) -> int:
        cleared = self._buffered_playback_bytes
        for utterance in self._buffered_playback:
            self._playback_provisional_gap_cancelled_bytes += (
                utterance.provisional_gap_bytes
            )
        if self._buffered_playback:
            self._playback_utterances_aborted += len(self._buffered_playback)
        self._buffered_playback.clear()
        self._buffered_playback_bytes = 0
        if teardown:
            return cleared
        return cleared

    def clear_playback(self) -> int:
        with self._pb_lock:
            cleared = self._playback_queued_bytes_locked()
            self._playback.clear()
            self._cancel_buffered_playback_locked()
            self._playback_cleared_bytes += cleared
        return cleared

    def media_metrics(self) -> dict[str, int | float | bool | str]:
        """Return exact per-stream capture/playback accounting."""

        with self._stream_lock:
            capture_queued = len(self._stream_capture)
            capture_metrics: dict[str, int | float | bool | str] = {
                "capture_queue_bytes": capture_queued,
                "capture_queue_ms": self._bytes_to_ms(capture_queued),
                "capture_queue_peak_bytes": self._capture_queue_peak_bytes,
                "capture_queue_peak_ms": self._bytes_to_ms(
                    self._capture_queue_peak_bytes
                ),
                "capture_capacity_bytes": CAPTURE_STREAM_MAX_BYTES,
                "capture_capacity_ms": self._bytes_to_ms(
                    CAPTURE_STREAM_MAX_BYTES
                ),
                "capture_received_bytes": self._capture_received_bytes,
                "capture_consumed_bytes": self._capture_consumed_bytes,
                "capture_overflow_bytes": self._capture_overflow_bytes,
                "capture_overflow_events": self._capture_overflow_events,
                "capture_teardown_discarded_bytes": (
                    self._capture_teardown_discarded_bytes
                ),
            }
        with self._pb_lock:
            queued = self._playback_queued_bytes_locked()
            peak = self._playback_queue_peak_bytes
            pending_gap_bytes = sum(
                item.provisional_gap_bytes for item in self._buffered_playback
            )
            playback_phase = (
                self._buffered_playback[0].phase
                if self._buffered_playback
                else "idle"
            )
            playback_metrics: dict[str, int | float | bool | str] = {
                "playback_queue_bytes": queued,
                "playback_queue_ms": self._bytes_to_ms(queued),
                "playback_queue_peak_bytes": peak,
                "playback_queue_peak_ms": self._bytes_to_ms(peak),
                "playback_capacity_bytes": PLAYBACK_MAX_BYTES,
                "playback_capacity_ms": PLAYBACK_BUFFER_MAX_MS,
                "playback_controlled": self._playback_controlled,
                "playback_phase": playback_phase,
                "playback_pending_utterances": len(self._buffered_playback),
                "playback_target_bytes": PLAYBACK_TARGET_BYTES,
                "playback_target_ms": PLAYBACK_TARGET_MS,
                "playback_accepted_bytes": self._playback_accepted_bytes,
                "playback_consumed_bytes": self._playback_consumed_bytes,
                "playback_cleared_bytes": self._playback_cleared_bytes,
                "playback_teardown_discarded_bytes": self._playback_teardown_discarded_bytes,
                "playback_rejected_bytes": self._playback_rejected_bytes,
                "playback_overflow_bytes": self._playback_overflow_bytes,
                "playback_overflow_events": self._playback_overflow_events,
                "playback_utterances_started": self._playback_utterances_started,
                "playback_utterances_completed": self._playback_utterances_completed,
                "playback_utterances_aborted": self._playback_utterances_aborted,
                "playback_short_utterances": self._playback_short_utterances,
                "playback_prebuffer_silence_bytes": (
                    self._playback_prebuffer_silence_bytes
                ),
                "playback_prebuffer_silence_packets": (
                    self._playback_prebuffer_silence_packets
                ),
                "playback_prebuffer_silence_ms": self._bytes_to_ms(
                    self._playback_prebuffer_silence_bytes
                ),
                "playback_rebuffer_completions": self._playback_rebuffer_completions,
                "playback_underrun_events": self._playback_underrun_events,
                "playback_underrun_silence_bytes": (
                    self._playback_underrun_silence_bytes
                ),
                "playback_underrun_silence_ms": self._bytes_to_ms(
                    self._playback_underrun_silence_bytes
                ),
                "playback_underrun_silence_packets": (
                    self._playback_underrun_silence_packets
                ),
                "playback_late_arrival_bytes": self._playback_late_arrival_bytes,
                "playback_provisional_gap_events": (
                    self._playback_provisional_gap_events
                ),
                "playback_provisional_gap_bytes": pending_gap_bytes,
                "playback_provisional_gap_ms": self._bytes_to_ms(
                    pending_gap_bytes
                ),
                "playback_provisional_gap_dismissed_bytes": (
                    self._playback_provisional_gap_dismissed_bytes
                ),
                "playback_provisional_gap_cancelled_bytes": (
                    self._playback_provisional_gap_cancelled_bytes
                ),
            }
        return {**capture_metrics, **playback_metrics}

    @staticmethod
    def _bytes_to_ms(byte_count: int) -> float:
        bytes_per_second = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2
        return round(byte_count * 1000 / bytes_per_second, 1)

    def queue_playback_b64(self, audio_b64: str) -> int:
        pcm = base64.b64decode(audio_b64)
        self.queue_playback(pcm)
        return len(pcm)


class AudioManager:
    """Registry of bounded logical leases over one physical SCO transport."""

    def __init__(
        self,
        health_callback: Optional[SCOHealthCallback] = None,
        *,
        max_sessions: int = MAX_LOGICAL_SESSIONS,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._sessions: dict[str, SCOAudioSession] = {}
        self._transport: Optional[_SCOTransport] = None
        self._health_callback = health_callback
        self._max_sessions = max_sessions
        self._retiring = False
        self._lock = threading.Lock()

    def create_session(self, session_id: str, phone_address: str) -> SCOAudioSession:
        address = _canonical_address(phone_address)
        with self._lock:
            if self._retiring:
                raise SCOAudioError("SCO transport teardown is still in progress")
            if session_id in self._sessions:
                raise ValueError(f"Session '{session_id}' already exists")
            if len(self._sessions) >= self._max_sessions:
                raise ValueError(
                    f"Maximum logical SCO sessions reached ({self._max_sessions})"
                )
            if self._transport is None:
                self._transport = _SCOTransport(address, self._health_callback)
            elif self._transport.address != address:
                raise SCOAudioError(
                    "Only one physical SCO phone can be active per manager "
                    f"({self._transport.address} is already leased)"
                )
            session = SCOAudioSession(
                session_id,
                address,
                _transport=self._transport,
            )
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> Optional[SCOAudioSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            transport = self._transport
            final = not self._sessions
            if final:
                self._retiring = True
        try:
            session.stop()
        finally:
            if final:
                with self._lock:
                    if not self._sessions and self._transport is transport:
                        self._transport = None
                    self._retiring = False
        return True

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def has_sessions(self) -> bool:
        """Return whether logical leases exist (legacy registry semantics)."""
        return self.session_count() > 0

    def has_active_transport(self) -> bool:
        """Return whether a verified physical SCO socket is currently healthy."""
        with self._lock:
            transport = self._transport
        return transport is not None and transport.is_connected()

    def health(self) -> dict[str, object]:
        with self._lock:
            transport = self._transport
        if transport is None:
            return SCOHealthEvent("stopped", "", 0).as_dict()
        return transport.health_event().as_dict()

    def stop_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            transport = self._transport
            if sessions:
                self._retiring = True
        try:
            for session in sessions:
                session.stop()
        finally:
            if sessions:
                with self._lock:
                    if not self._sessions and self._transport is transport:
                        self._transport = None
                    self._retiring = False
        return len(sessions)
