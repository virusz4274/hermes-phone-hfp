"""
SCO audio capture and playback via sounddevice (PipeWire / ALSA backend).

AudioSession manages one call's worth of audio I/O:
  - InputStream  captures phone microphone → capture ring buffer
  - OutputStream plays TTS audio → phone speaker

AudioManager is a thread-safe registry of active sessions.

Audio format: 8 kHz, 16-bit signed mono PCM (HFP CVSD standard).
"""

from __future__ import annotations

import base64
import logging
import threading
from collections import deque
from typing import Optional

import numpy as np
import sounddevice as sd

from ..config import (
    AUDIO_BUFFER_MAX_CHUNKS,
    AUDIO_CHANNELS,
    AUDIO_CHUNK_FRAMES,
    AUDIO_DTYPE,
    AUDIO_SAMPLE_RATE,
)

log = logging.getLogger(__name__)


class AudioSession:
    """
    Manages SCO capture + playback for one logical call session.

    Capture buffer: collections.deque (thread-safe single-producer/consumer).
    Playback buffer: deque guarded by a Lock (multiple asyncio callers may
    call queue_playback concurrently via run_in_executor).
    """

    def __init__(self, session_id: str, source_name: str, sink_name: str) -> None:
        self.session_id = session_id
        self._source = source_name
        self._sink = sink_name

        self._capture_buf: deque[bytes] = deque(maxlen=AUDIO_BUFFER_MAX_CHUNKS)
        self._playback_buf: deque[np.ndarray] = deque()
        self._pb_lock = threading.Lock()

        self._in_stream: Optional[sd.InputStream] = None
        self._out_stream: Optional[sd.OutputStream] = None

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._in_stream = sd.InputStream(
            device=self._source,
            samplerate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=AUDIO_CHUNK_FRAMES,
            callback=self._capture_cb,
        )
        self._out_stream = sd.OutputStream(
            device=self._sink,
            samplerate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=AUDIO_CHUNK_FRAMES,
            callback=self._playback_cb,
        )
        self._in_stream.start()
        self._out_stream.start()
        log.info("Audio session %s started (src=%s, snk=%s)", self.session_id, self._source, self._sink)

    def stop(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    log.debug("Stream close error: %s", exc)
        self._in_stream = None
        self._out_stream = None
        log.info("Audio session %s stopped", self.session_id)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_cb(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning("Capture status: %s", status)
        self._capture_buf.append(indata.tobytes())

    def get_chunk(self) -> Optional[bytes]:
        """Pop the oldest captured PCM chunk, or None if buffer is empty."""
        try:
            return self._capture_buf.popleft()
        except IndexError:
            return None

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _playback_cb(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning("Playback status: %s", status)
        with self._pb_lock:
            if self._playback_buf:
                chunk = self._playback_buf.popleft()
                needed = frames * AUDIO_CHANNELS
                if len(chunk) < needed:
                    chunk = np.pad(chunk, (0, needed - len(chunk)))
                outdata[:] = chunk[:needed].reshape(outdata.shape)
            else:
                outdata.fill(0)   # silence when nothing queued

    def queue_playback(self, pcm_bytes: bytes) -> None:
        """
        Queue raw PCM bytes for playback into the call.

        Splits the audio into AUDIO_CHUNK_FRAMES-sized slices so that the
        playback callback (which processes one slice per invocation) plays
        the full audio rather than truncating at the first chunk boundary.
        """
        arr = np.frombuffer(pcm_bytes, dtype=AUDIO_DTYPE)
        with self._pb_lock:
            for i in range(0, max(len(arr), 1), AUDIO_CHUNK_FRAMES):
                self._playback_buf.append(arr[i : i + AUDIO_CHUNK_FRAMES])

    # ------------------------------------------------------------------
    # Convenience helpers (used by MCP tools)
    # ------------------------------------------------------------------

    def get_chunk_b64(self) -> Optional[str]:
        chunk = self.get_chunk()
        return base64.b64encode(chunk).decode("ascii") if chunk else None

    def queue_playback_b64(self, audio_b64: str) -> int:
        pcm = base64.b64decode(audio_b64)
        self.queue_playback(pcm)
        return len(pcm)


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

class AudioManager:
    """Thread-safe registry of AudioSession objects keyed by session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, AudioSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self, session_id: str, source: str, sink: str
    ) -> AudioSession:
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session '{session_id}' already exists")
            s = AudioSession(session_id, source, sink)
            self._sessions[session_id] = s
            return s

    def get_session(self, session_id: str) -> Optional[AudioSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s:
            s.stop()

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.stop()
