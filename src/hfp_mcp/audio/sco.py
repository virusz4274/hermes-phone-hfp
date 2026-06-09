"""
SCO audio bridge for one HFP call.

Because this server registers its *own* ``org.bluez.Profile1`` and owns the HFP
service-level connection (the RFCOMM/AT channel), PipeWire's bluez5 backend never
manages the phone and never creates ``bluez_input.*`` / ``bluez_output.*`` nodes.
So we cannot route call audio through PipeWire — we own the SCO link directly.

We open a Bluetooth ``SOCK_SEQPACKET`` / ``BTPROTO_SCO`` socket and connect it to
the phone (HF-initiated Setup Synchronous Connection).  With the CVSD codec
(mSBC disabled) the controller does the codec and the socket carries plain
8 kHz, 16-bit signed mono PCM — exactly the format the MCP audio tools expose.

Pacing
──────
SCO is isochronous: the controller delivers one small frame (≤ SCO MTU, 64 bytes
here) roughly every 4 ms, and the link underruns if we ever stop feeding it.  We
use a single duplex loop *clocked by RX*: each time we ``recv`` one frame we
immediately ``send`` one frame of the same size — drawn from the playback buffer
or zero-filled with silence.  This keeps TX correctly paced and the air link
continuously fed without a separate timer thread.
"""

from __future__ import annotations

import base64
import logging
import socket
import struct
import threading
import time
from collections import deque
from typing import Optional

from ..config import (
    AUDIO_BUFFER_MAX_CHUNKS,
    AUDIO_CHANNELS,
    AUDIO_CHUNK_FRAMES,
    AUDIO_SAMPLE_RATE,
    AUDIO_STREAM_FRAME_MS,
)

log = logging.getLogger(__name__)

# Raw bytes per ~200 ms chunk: frames × channels × 2 (int16 = 2 bytes/sample).
CHUNK_BYTES: int = AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2
STREAM_FRAME_BYTES: int = (
    AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 * AUDIO_STREAM_FRAME_MS // 1000
)

# Cap the outbound (playback) buffer so injected audio can't build up unbounded
# latency — drop the oldest bytes past this many chunks' worth.
PLAYBACK_MAX_BYTES: int = AUDIO_BUFFER_MAX_CHUNKS * CHUNK_BYTES

# Bind any local adapter; the kernel routes SCO via the controller that owns the
# ACL link to the phone.
_BDADDR_ANY = "00:00:00:00:00:00"

# Default read size — one SCO datagram is far smaller (≤ 64 B), but recv on a
# SEQPACKET socket returns exactly one frame regardless of the buffer size.
_RECV_BUF = 1024

# getsockopt(SOL_SCO, SCO_OPTIONS) → struct sco_options { uint16 mtu }
_SOL_SCO = 17
_SCO_OPTIONS = 1


class SCOAudioError(RuntimeError):
    """Raised when the SCO audio link cannot be established."""


class SCOAudioSession:
    """
    Owns the SCO socket and the duplex audio bridge for one logical call session.

    Public interface mirrors the previous PipeWire-backed ``AudioSession`` so the
    MCP tools and ``AudioManager`` are unchanged:

      capture:   ``get_chunk`` / ``get_chunk_b64`` pop ~200 ms PCM chunks captured
                 from the phone microphone.
      playback:  ``queue_playback`` / ``queue_playback_b64`` feed PCM that the
                 remote party hears.
    """

    def __init__(self, session_id: str, phone_address: str) -> None:
        self.session_id = session_id
        self._address = phone_address
        self.mtu: Optional[int] = None

        # capture ring (bridge thread = producer, asyncio = consumer)
        self._capture_buf: deque[bytes] = deque(maxlen=AUDIO_BUFFER_MAX_CHUNKS)
        self._capture_accum = bytearray()
        self._stream_capture = bytearray()
        self._stream_lock = threading.Lock()
        # outbound playback bytes (asyncio callers = producer, bridge = consumer)
        self._playback = bytearray()
        self._pb_lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._bridge: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self, connect_timeout: float = 5.0) -> None:
        """Connect the SCO link (with retry) and start the bridge thread."""
        self._sock = self._connect_sco(connect_timeout)
        self.mtu = _read_sco_mtu(self._sock)
        self._bridge = threading.Thread(
            target=self._bridge_loop, daemon=True, name=f"sco-{self.session_id}"
        )
        self._bridge.start()
        log.info(
            "SCO audio session %s up (peer=%s, mtu=%s)",
            self.session_id, self._address, self.mtu,
        )

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        bridge = self._bridge
        if bridge is not None and bridge.is_alive():
            bridge.join(timeout=2.0)
        log.info("SCO audio session %s stopped", self.session_id)

    # ------------------------------------------------------------------
    # SCO connection
    # ------------------------------------------------------------------

    def _connect_sco(self, timeout: float) -> socket.socket:
        """
        HF-initiated SCO setup, retried until the AG accepts or `timeout` elapses.

        The phone may still be wiring up the call leg when the call first reports
        ACTIVE, so an immediate connect can fail with EAGAIN/ECONNREFUSED; we poll.
        """
        deadline = time.monotonic() + timeout
        last_exc: Optional[BaseException] = None
        while True:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO
            )
            try:
                sock.bind(_BDADDR_ANY)
                sock.settimeout(max(0.5, deadline - time.monotonic()))
                sock.connect(self._address)
                sock.settimeout(None)  # blocking recv drives the bridge clock
                return sock
            except OSError as exc:
                last_exc = exc
                try:
                    sock.close()
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
        raise SCOAudioError(
            f"Could not establish SCO audio link to {self._address}: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Duplex bridge (one thread, RX-paced)
    # ------------------------------------------------------------------

    def _bridge_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                frame = sock.recv(_RECV_BUF)
            except OSError as exc:
                if not self._stop.is_set():
                    log.info("SCO recv ended for %s: %s", self.session_id, exc)
                break
            if not frame:
                if not self._stop.is_set():
                    log.info("SCO link closed for %s", self.session_id)
                break

            self._absorb_capture(frame)

            # Send one frame back, matching the received size (always ≤ MTU and
            # naturally paced by the controller's RX cadence).
            out = self._take_playback(len(frame))
            try:
                sock.send(out)
            except OSError as exc:
                if not self._stop.is_set():
                    log.info("SCO send ended for %s: %s", self.session_id, exc)
                break

    def _absorb_capture(self, frame: bytes) -> None:
        """Accumulate raw SCO bytes into fixed ~200 ms chunks for the capture ring."""
        self._capture_accum.extend(frame)
        while len(self._capture_accum) >= CHUNK_BYTES:
            chunk = bytes(self._capture_accum[:CHUNK_BYTES])
            del self._capture_accum[:CHUNK_BYTES]
            self._capture_buf.append(chunk)  # deque.append is atomic
        with self._stream_lock:
            self._stream_capture.extend(frame)
            overflow = len(self._stream_capture) - PLAYBACK_MAX_BYTES
            if overflow > 0:
                del self._stream_capture[:overflow]

    def _take_playback(self, n: int) -> bytes:
        """Pop up to `n` queued playback bytes, zero-padded to exactly `n` (silence)."""
        with self._pb_lock:
            if self._playback:
                take = self._playback[:n]
                del self._playback[:n]
            else:
                take = b""
        if len(take) < n:
            return bytes(take) + b"\x00" * (n - len(take))
        return bytes(take)

    # ------------------------------------------------------------------
    # Capture API
    # ------------------------------------------------------------------

    def get_chunk(self) -> Optional[bytes]:
        """Pop the oldest captured PCM chunk, or None if the buffer is empty."""
        try:
            return self._capture_buf.popleft()
        except IndexError:
            return None

    def get_chunk_b64(self) -> Optional[str]:
        chunk = self.get_chunk()
        return base64.b64encode(chunk).decode("ascii") if chunk else None

    def pop_stream_frame(self, n: int = STREAM_FRAME_BYTES) -> Optional[bytes]:
        """Pop one low-latency PCM frame for realtime sidecar streaming."""
        with self._stream_lock:
            if len(self._stream_capture) < n:
                return None
            frame = bytes(self._stream_capture[:n])
            del self._stream_capture[:n]
            return frame

    # ------------------------------------------------------------------
    # Playback API
    # ------------------------------------------------------------------

    def queue_playback(self, pcm_bytes: bytes) -> None:
        """Queue raw PCM bytes for the remote party to hear (8 kHz s16le mono)."""
        if not pcm_bytes:
            return
        if self._sock is None:
            raise RuntimeError("SCO audio session is not running")
        with self._pb_lock:
            self._playback.extend(pcm_bytes)
            overflow = len(self._playback) - PLAYBACK_MAX_BYTES
            if overflow > 0:
                # Drop the oldest queued audio to bound added latency.
                del self._playback[:overflow]

    def clear_playback(self) -> int:
        """Drop queued outbound audio and return the number of bytes cleared."""
        with self._pb_lock:
            cleared = len(self._playback)
            self._playback.clear()
        return cleared

    def queue_playback_b64(self, audio_b64: str) -> int:
        pcm = base64.b64decode(audio_b64)
        self.queue_playback(pcm)
        return len(pcm)


def _read_sco_mtu(sock: socket.socket) -> Optional[int]:
    """Best-effort read of the negotiated SCO MTU; None if the kernel won't tell us."""
    try:
        raw = sock.getsockopt(_SOL_SCO, _SCO_OPTIONS, 2)
        return struct.unpack("H", raw)[0]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

class AudioManager:
    """Thread-safe registry of SCOAudioSession objects keyed by session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, SCOAudioSession] = {}
        self._lock = threading.Lock()

    def create_session(self, session_id: str, phone_address: str) -> SCOAudioSession:
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session '{session_id}' already exists")
            s = SCOAudioSession(session_id, phone_address)
            self._sessions[session_id] = s
            return s

    def get_session(self, session_id: str) -> Optional[SCOAudioSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> bool:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s:
            s.stop()
            return True
        return False

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def has_sessions(self) -> bool:
        return self.session_count() > 0

    def stop_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.stop()
        return len(sessions)
