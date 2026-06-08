"""
SCO audio capture and playback for one HFP call.

We route audio with the PulseAudio/PipeWire CLI tools (``parec`` / ``pacat``)
rather than PortAudio (sounddevice).  The HFP SCO link is exposed as a PipeWire
node addressed by a PulseAudio-domain name (e.g. ``bluez_input.AA_BB_..._.0``)
— exactly the name ``PipeWireDeviceLocator`` discovers via ``pactl``.  PortAudio
cannot address an individual Pulse/PipeWire node by that name (it sees only the
ALSA/`default`/`pulse` device), so passing such a name to it either fails or
silently captures the wrong source.  ``parec``/``pacat`` take the node name with
``-d`` and route to the right node every time.

Audio format: 8 kHz, 16-bit signed mono PCM (HFP CVSD standard).
"""

from __future__ import annotations

import base64
import logging
import subprocess
import threading
from collections import deque
from typing import Optional

from ..config import (
    AUDIO_BUFFER_MAX_CHUNKS,
    AUDIO_CHANNELS,
    AUDIO_CHUNK_FRAMES,
    AUDIO_SAMPLE_RATE,
)

log = logging.getLogger(__name__)

# Raw bytes per ~200 ms chunk: frames × channels × 2 (int16 = 2 bytes/sample).
CHUNK_BYTES: int = AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2


def _parec_cmd(device: str) -> list[str]:
    """Build the ``parec`` (record) command for an 8 kHz s16le mono raw stream."""
    return [
        "parec",
        "-d", device,
        "--format=s16le",
        f"--rate={AUDIO_SAMPLE_RATE}",
        f"--channels={AUDIO_CHANNELS}",
        "--raw",
    ]


def _pacat_cmd(device: str) -> list[str]:
    """Build the ``pacat`` (playback) command for an 8 kHz s16le mono raw stream."""
    return [
        "pacat",
        "--playback",
        "-d", device,
        "--format=s16le",
        f"--rate={AUDIO_SAMPLE_RATE}",
        f"--channels={AUDIO_CHANNELS}",
        "--raw",
    ]


class AudioSession:
    """
    Manages SCO capture + playback for one logical call session.

    Capture: ``parec`` writes raw PCM to stdout; a reader thread slices it into
    fixed-size chunks and appends to a bounded ring buffer (deque with maxlen —
    thread-safe for single-producer/consumer).
    Playback: ``pacat`` reads raw PCM from stdin; queue_playback writes bytes
    straight to it (guarded by a lock since multiple asyncio callers may write
    concurrently via run_in_executor).  PipeWire handles playback buffering.
    """

    def __init__(self, session_id: str, source_name: str, sink_name: str) -> None:
        self.session_id = session_id
        self._source = source_name
        self._sink = sink_name

        self._capture_buf: deque[bytes] = deque(maxlen=AUDIO_BUFFER_MAX_CHUNKS)
        self._pb_lock = threading.Lock()
        self._stop = threading.Event()

        self._rec_proc: Optional[subprocess.Popen] = None
        self._play_proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._rec_proc = subprocess.Popen(
            _parec_cmd(self._source),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._play_proc = subprocess.Popen(
            _pacat_cmd(self._sink),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(
            target=self._capture_loop, daemon=True, name=f"audio-rx-{self.session_id}"
        )
        self._reader.start()
        log.info(
            "Audio session %s started (src=%s, snk=%s)",
            self.session_id, self._source, self._sink,
        )

    def stop(self) -> None:
        self._stop.set()
        with self._pb_lock:
            play, self._play_proc = self._play_proc, None
        rec, self._rec_proc = self._rec_proc, None
        if play is not None:
            try:
                if play.stdin:
                    play.stdin.close()
            except OSError:
                pass
            _terminate(play)
        if rec is not None:
            _terminate(rec)
        log.info("Audio session %s stopped", self.session_id)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        proc = self._rec_proc
        if proc is None or proc.stdout is None:
            return
        while not self._stop.is_set():
            # BufferedReader.read(n) returns exactly n bytes until EOF, giving
            # uniform ~200 ms chunks.
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                if not self._stop.is_set():
                    log.info("Audio capture stream ended for %s", self.session_id)
                break
            self._capture_buf.append(data)

    def get_chunk(self) -> Optional[bytes]:
        """Pop the oldest captured PCM chunk, or None if buffer is empty."""
        try:
            return self._capture_buf.popleft()
        except IndexError:
            return None

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def queue_playback(self, pcm_bytes: bytes) -> None:
        """Write raw PCM bytes to the playback stream (heard by the remote party)."""
        if not pcm_bytes:
            return
        with self._pb_lock:
            proc = self._play_proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("Playback stream is not running")
            try:
                proc.stdin.write(pcm_bytes)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"Playback stream write failed: {exc}") from exc

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


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate a child process, escalating to kill if it doesn't exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            log.warning("Audio subprocess (pid %s) did not exit", proc.pid)


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
