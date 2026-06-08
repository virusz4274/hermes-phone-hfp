"""
Tests for the SCO audio bridge plumbing.

These cover the capture-ring chunking, the playback buffer / silence-fill, and
the session registry without opening a real SCO socket or touching Bluetooth.
"""

import base64

from hfp_mcp.audio.sco import (
    CHUNK_BYTES,
    AudioManager,
    SCOAudioSession,
)
from hfp_mcp.config import AUDIO_CHANNELS, AUDIO_CHUNK_FRAMES


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------

def test_chunk_bytes_matches_format():
    # 200 ms @ 8 kHz mono, int16 (2 bytes/sample)
    assert CHUNK_BYTES == AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2


# ---------------------------------------------------------------------------
# Capture ring (no socket)
# ---------------------------------------------------------------------------

def test_get_chunk_returns_none_when_empty():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    assert s.get_chunk() is None
    assert s.get_chunk_b64() is None


def test_absorb_capture_accumulates_into_chunks():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    # Feed one full chunk's worth in small SCO-sized frames (48 B each).
    payload = bytes((i % 256 for i in range(CHUNK_BYTES)))
    for off in range(0, CHUNK_BYTES, 48):
        s._absorb_capture(payload[off:off + 48])
    # Exactly one chunk should be available, byte-identical to what went in.
    chunk = s.get_chunk()
    assert chunk == payload
    assert s.get_chunk() is None  # remainder (< CHUNK_BYTES) not yet emitted


def test_get_chunk_b64_roundtrip():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    pcm = bytes((i % 256 for i in range(CHUNK_BYTES)))
    s._absorb_capture(pcm)
    b64 = s.get_chunk_b64()
    assert base64.b64decode(b64) == pcm
    assert s.get_chunk() is None


# ---------------------------------------------------------------------------
# Playback buffer / silence fill (no socket)
# ---------------------------------------------------------------------------

def test_queue_playback_without_link_raises():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    try:
        s.queue_playback(b"\x00\x01")
        assert False, "expected RuntimeError when SCO link not running"
    except RuntimeError:
        pass


def test_queue_playback_empty_is_noop():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    s.queue_playback(b"")  # must not raise even though the link is down


def test_take_playback_silence_when_empty():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    assert s._take_playback(48) == b"\x00" * 48


def test_take_playback_pads_partial_frame_with_silence():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    with s._pb_lock:
        s._playback.extend(b"\x11\x22\x33")
    out = s._take_playback(6)
    assert out == b"\x11\x22\x33\x00\x00\x00"
    # buffer drained
    assert s._take_playback(2) == b"\x00\x00"


def test_take_playback_returns_queued_bytes_in_order():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    with s._pb_lock:
        s._playback.extend(bytes(range(10)))
    assert s._take_playback(4) == bytes([0, 1, 2, 3])
    assert s._take_playback(4) == bytes([4, 5, 6, 7])
    assert s._take_playback(4) == bytes([8, 9, 0, 0])  # padded


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_manager_create_get_remove():
    m = AudioManager()
    s = m.create_session("call1", "AA:BB:CC:DD:EE:FF")
    assert m.get_session("call1") is s
    m.remove_session("call1")  # stop() is a no-op since nothing started
    assert m.get_session("call1") is None


def test_manager_rejects_duplicate_session():
    m = AudioManager()
    m.create_session("call1", "AA:BB:CC:DD:EE:FF")
    try:
        m.create_session("call1", "AA:BB:CC:DD:EE:FF")
        assert False, "expected ValueError on duplicate session"
    except ValueError:
        pass
