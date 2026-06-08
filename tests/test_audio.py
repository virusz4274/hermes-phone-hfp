"""
Tests for the parec/pacat audio routing helpers.

These cover command construction and the capture/playback buffer plumbing
without spawning real subprocesses or touching audio hardware.
"""

import base64

from hfp_mcp.audio.capture import (
    CHUNK_BYTES,
    AudioManager,
    AudioSession,
    _pacat_cmd,
    _parec_cmd,
)
from hfp_mcp.config import AUDIO_CHANNELS, AUDIO_CHUNK_FRAMES


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def test_chunk_bytes_matches_format():
    # 200 ms @ 8 kHz mono, int16 (2 bytes/sample)
    assert CHUNK_BYTES == AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2


def test_parec_cmd_targets_node_and_format():
    cmd = _parec_cmd("bluez_input.AA_BB_CC_DD_EE_FF.0")
    assert cmd[0] == "parec"
    # node name passed via -d
    assert "-d" in cmd
    assert cmd[cmd.index("-d") + 1] == "bluez_input.AA_BB_CC_DD_EE_FF.0"
    assert "--format=s16le" in cmd
    assert "--rate=8000" in cmd
    assert "--channels=1" in cmd
    assert "--raw" in cmd


def test_pacat_cmd_targets_node_and_format():
    cmd = _pacat_cmd("bluez_output.AA_BB_CC_DD_EE_FF.0")
    assert cmd[0] == "pacat"
    assert "--playback" in cmd
    assert cmd[cmd.index("-d") + 1] == "bluez_output.AA_BB_CC_DD_EE_FF.0"
    assert "--format=s16le" in cmd
    assert "--rate=8000" in cmd
    assert "--channels=1" in cmd
    assert "--raw" in cmd


# ---------------------------------------------------------------------------
# Capture buffer plumbing (no subprocess)
# ---------------------------------------------------------------------------

def test_get_chunk_returns_none_when_empty():
    s = AudioSession("call1", "src", "snk")
    assert s.get_chunk() is None
    assert s.get_chunk_b64() is None


def test_get_chunk_b64_roundtrip():
    s = AudioSession("call1", "src", "snk")
    pcm = b"\x01\x02\x03\x04"
    s._capture_buf.append(pcm)
    b64 = s.get_chunk_b64()
    assert base64.b64decode(b64) == pcm
    # buffer drained
    assert s.get_chunk() is None


def test_queue_playback_without_stream_raises():
    s = AudioSession("call1", "src", "snk")
    try:
        s.queue_playback(b"\x00\x01")
        assert False, "expected RuntimeError when playback stream not running"
    except RuntimeError:
        pass


def test_queue_playback_empty_is_noop():
    s = AudioSession("call1", "src", "snk")
    # empty input must not touch the (absent) stream
    s.queue_playback(b"")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_manager_create_get_remove():
    m = AudioManager()
    s = m.create_session("call1", "src", "snk")
    assert m.get_session("call1") is s
    m.remove_session("call1")  # stop() is a no-op since nothing started
    assert m.get_session("call1") is None


def test_manager_rejects_duplicate_session():
    m = AudioManager()
    m.create_session("call1", "src", "snk")
    try:
        m.create_session("call1", "src", "snk")
        assert False, "expected ValueError on duplicate session"
    except ValueError:
        pass
