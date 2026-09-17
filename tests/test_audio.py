"""
Tests for the SCO audio bridge plumbing.

These cover the capture-ring chunking, the playback buffer / silence-fill, and
the session registry without opening a real SCO socket or touching Bluetooth.
"""

import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient, WebSocketDisconnect

from hfp_mcp.audio.sco import (
    CAPTURE_STREAM_MAX_BYTES,
    CHUNK_BYTES,
    PLAYBACK_TARGET_BYTES,
    STREAM_FRAME_BYTES,
    AudioManager,
    SCOAudioSession,
)
from hfp_mcp.audio.sidecar import AudioStreamServer
import hfp_mcp.audio.sidecar as sidecar_mod
from hfp_mcp.config import AUDIO_CHANNELS, AUDIO_CHUNK_FRAMES


class _ConnectedTransport:
    def is_connected(self):
        return True


def _running_session(session_id="call1"):
    session = SCOAudioSession(
        session_id,
        "AA:BB:CC:DD:EE:FF",
        _transport=_ConnectedTransport(),
    )
    with session._health_lock:
        session._running = True
    return session


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------

def test_chunk_bytes_matches_format():
    # 200 ms @ 8 kHz mono, int16 (2 bytes/sample)
    assert CHUNK_BYTES == AUDIO_CHUNK_FRAMES * AUDIO_CHANNELS * 2


def test_stream_frame_bytes_is_low_latency():
    # 20 ms @ 8 kHz mono, int16 (2 bytes/sample)
    assert STREAM_FRAME_BYTES == 320


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


def test_pop_stream_frame_returns_small_pcm_frames():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    pcm = bytes((i % 256 for i in range(STREAM_FRAME_BYTES * 2)))
    s._absorb_capture(pcm)

    assert s.pop_stream_frame() == pcm[:STREAM_FRAME_BYTES]
    assert s.pop_stream_frame() == pcm[STREAM_FRAME_BYTES:]
    assert s.pop_stream_frame() is None


def test_capture_stream_metrics_account_for_every_received_byte():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    received = b"x" * (CAPTURE_STREAM_MAX_BYTES + 100)

    s._absorb_capture(received)
    assert s.pop_stream_frame() == b"x" * STREAM_FRAME_BYTES

    before_teardown = s.media_metrics()
    assert before_teardown["capture_received_bytes"] == len(received)
    assert before_teardown["capture_consumed_bytes"] == STREAM_FRAME_BYTES
    assert before_teardown["capture_overflow_bytes"] == 100
    assert before_teardown["capture_overflow_events"] == 1
    assert before_teardown["capture_queue_bytes"] == (
        CAPTURE_STREAM_MAX_BYTES - STREAM_FRAME_BYTES
    )
    assert before_teardown["capture_queue_peak_bytes"] == CAPTURE_STREAM_MAX_BYTES

    s._clear_buffers()
    after_teardown = s.media_metrics()
    assert after_teardown["capture_queue_bytes"] == 0
    assert after_teardown["capture_teardown_discarded_bytes"] == (
        CAPTURE_STREAM_MAX_BYTES - STREAM_FRAME_BYTES
    )
    assert after_teardown["capture_received_bytes"] == (
        after_teardown["capture_consumed_bytes"]
        + after_teardown["capture_overflow_bytes"]
        + after_teardown["capture_teardown_discarded_bytes"]
    )


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


def test_clear_playback_drops_queued_bytes():
    s = SCOAudioSession("call1", "AA:BB:CC:DD:EE:FF")
    with s._pb_lock:
        s._playback.extend(b"abcdef")

    assert s.clear_playback() == 6
    assert s._take_playback(2) == b"\x00\x00"


def test_controlled_playback_waits_for_target_without_counting_underrun():
    s = _running_session()
    frame = b"\x01\x00" * (STREAM_FRAME_BYTES // 2)
    s.start_buffered_playback("utterance-1")
    for _ in range((PLAYBACK_TARGET_BYTES // STREAM_FRAME_BYTES) - 1):
        s.queue_playback(frame)

    assert s._take_playback_raw(64) == b""
    waiting = s.media_metrics()
    assert waiting["playback_prebuffer_silence_bytes"] == 64
    assert waiting["playback_underrun_events"] == 0

    s.queue_playback(frame)
    assert s._take_playback_raw(64) == frame[:64]
    playing = s.media_metrics()
    assert playing["playback_target_bytes"] == PLAYBACK_TARGET_BYTES
    assert playing["playback_controlled"] is True


def test_controlled_short_utterance_plays_on_end_without_false_underrun():
    s = _running_session()
    pcm = bytes(range(64)) * 5
    s.start_buffered_playback("short")
    s.queue_playback(pcm)
    s.end_buffered_playback("short")

    assert s._take_playback_raw(128) == pcm[:128]
    assert s._take_playback_raw(128) == pcm[128:256]
    assert s._take_playback_raw(128) == pcm[256:]
    assert s._take_playback_raw(128) == b""
    metrics = s.media_metrics()
    assert metrics["playback_short_utterances"] == 1
    assert metrics["playback_utterances_completed"] == 1
    assert metrics["playback_underrun_events"] == 0


def test_playback_end_dismisses_a_provisional_tail_gap():
    s = _running_session()
    s.start_buffered_playback("tail")
    s.queue_playback(b"a" * PLAYBACK_TARGET_BYTES)
    for _ in range(PLAYBACK_TARGET_BYTES // 64):
        assert s._take_playback_raw(64) == b"a" * 64

    assert s._take_playback_raw(64) == b""
    assert s.media_metrics()["playback_provisional_gap_bytes"] == 64
    assert s.media_metrics()["playback_underrun_events"] == 0

    s.end_buffered_playback("tail")
    assert s._take_playback_raw(64) == b""
    metrics = s.media_metrics()
    assert metrics["playback_provisional_gap_bytes"] == 0
    assert metrics["playback_provisional_gap_dismissed_bytes"] == 64
    assert metrics["playback_underrun_events"] == 0


def test_late_pcm_confirms_gap_and_rebuffers_before_resuming():
    s = _running_session()
    s.start_buffered_playback("late")
    s.queue_playback(b"a" * PLAYBACK_TARGET_BYTES)
    for _ in range(PLAYBACK_TARGET_BYTES // 64):
        s._take_playback_raw(64)

    assert s._take_playback_raw(64) == b""
    assert s._take_playback_raw(64) == b""
    s.queue_playback(b"b" * STREAM_FRAME_BYTES)
    confirmed = s.media_metrics()
    assert confirmed["playback_underrun_events"] == 1
    assert confirmed["playback_underrun_silence_bytes"] == 128
    assert confirmed["playback_late_arrival_bytes"] == STREAM_FRAME_BYTES

    assert s._take_playback_raw(64) == b""
    s.queue_playback(b"c" * (PLAYBACK_TARGET_BYTES - STREAM_FRAME_BYTES))
    assert s._take_playback_raw(64) == b"b" * 64
    resumed = s.media_metrics()
    assert resumed["playback_underrun_events"] == 1
    assert resumed["playback_underrun_silence_bytes"] == 192
    assert resumed["playback_rebuffer_completions"] == 1


def test_clear_playback_aborts_controlled_audio_without_false_underrun():
    s = _running_session()
    s.start_buffered_playback("interrupted")
    s.queue_playback(b"a" * PLAYBACK_TARGET_BYTES)

    assert s.clear_playback() == PLAYBACK_TARGET_BYTES
    assert s._take_playback_raw(64) == b""
    metrics = s.media_metrics()
    assert metrics["playback_queue_bytes"] == 0
    assert metrics["playback_utterances_aborted"] == 1
    assert metrics["playback_underrun_events"] == 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_manager_create_get_remove():
    m = AudioManager()
    s = m.create_session("call1", "AA:BB:CC:DD:EE:FF")
    assert m.get_session("call1") is s
    assert m.session_count() == 1
    assert m.has_sessions() is True
    assert m.remove_session("call1") is True  # stop() is a no-op since nothing started
    assert m.get_session("call1") is None
    assert m.session_count() == 0
    assert m.has_sessions() is False
    assert m.remove_session("call1") is False


def test_manager_rejects_duplicate_session():
    m = AudioManager()
    m.create_session("call1", "AA:BB:CC:DD:EE:FF")
    try:
        m.create_session("call1", "AA:BB:CC:DD:EE:FF")
        assert False, "expected ValueError on duplicate session"
    except ValueError:
        pass


def test_manager_stop_all_returns_stopped_count():
    m = AudioManager()
    m.create_session("call1", "AA:BB:CC:DD:EE:FF")
    m.create_session("call2", "AA:BB:CC:DD:EE:FF")

    assert m.stop_all() == 2
    assert m.session_count() == 0
    assert m.stop_all() == 0


def test_audio_stream_server_issues_session_tokens_and_metadata():
    m = AudioManager()
    server = AudioStreamServer(m, "127.0.0.1", 8765)
    token = server.issue_token("call1")

    assert token
    assert server._consume_token("call1", token) is not None
    assert server._consume_token("call1", token) is None
    assert server._consume_token("call1", "wrong") is None
    assert "/audio/call1?token=" in server.stream_url("call1", token)
    assert server.metadata()["frame_bytes"] == STREAM_FRAME_BYTES


def test_gemini_sidecar_controls_and_pcm_are_processed_in_wire_order():
    manager = AudioManager()
    session = manager.create_session("call1", "AA:BB:CC:DD:EE:FF")
    received = []
    session.start_buffered_playback = lambda utterance_id=None: received.append(
        ("start", utterance_id)
    )
    session.queue_playback = lambda pcm: received.append(("pcm", bytes(pcm)))
    session.end_buffered_playback = lambda utterance_id=None: received.append(
        ("end", utterance_id)
    )
    server = AudioStreamServer(manager, "127.0.0.1", 8765, embedded=True)
    token = server.issue_token("call1", owner="gemini_live")
    app = Starlette(routes=server.routes())

    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/call1?token={token}") as websocket:
            websocket.send_text(
                json.dumps(
                    {"type": "playback_start", "utterance_id": "generation-7"}
                )
            )
            websocket.send_bytes(b"\x01\x00\x02\x00")
            websocket.send_text(
                json.dumps(
                    {"type": "playback_end", "utterance_id": "generation-7"}
                )
            )

    assert received == [
        ("start", "generation-7"),
        ("pcm", b"\x01\x00\x02\x00"),
        ("end", "generation-7"),
    ]


def test_non_gemini_sidecar_owner_remains_binary_only():
    manager = AudioManager()
    manager.create_session("call1", "AA:BB:CC:DD:EE:FF")
    server = AudioStreamServer(manager, "127.0.0.1", 8765, embedded=True)
    token = server.issue_token("call1", owner="mcp_client")
    app = Starlette(routes=server.routes())

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                f"/audio/call1?token={token}"
            ) as websocket:
                websocket.send_text('{"type":"playback_start"}')
                websocket.receive_bytes()

    assert closed.value.code == 1003


def test_audio_stream_server_reissue_revokes_every_older_token():
    server = AudioStreamServer(AudioManager(), "127.0.0.1", 8765)
    first = server.issue_token("call1")
    second = server.issue_token("call1")

    assert first != second
    assert server._consume_token("call1", first) is None
    assert server._consume_token("call1", first) is None
    assert server._consume_token("call1", second) is not None


def test_audio_stream_server_uses_external_tls_origin_without_backend_port():
    server = AudioStreamServer(
        AudioManager(),
        "127.0.0.1",
        8000,
        public_base_url="https://phone.example.lan",
    )
    token = server.issue_token("call1")

    assert server.stream_url("call1", token).startswith(
        "wss://phone.example.lan/audio/call1?token="
    )


def test_audio_stream_server_revokes_tokens_when_session_detaches():
    server = AudioStreamServer(AudioManager(), "127.0.0.1", 8765)
    token = server.issue_token("call1")
    server.detach_session("call1")
    assert server._consume_token("call1", token) is None


def test_audio_stream_server_can_reject_a_stale_bound_grant():
    accepted = []
    server = AudioStreamServer(
        AudioManager(),
        "127.0.0.1",
        8765,
        grant_validator=lambda session_id, grant: (
            accepted.append((session_id, grant.call_id)) or False
        ),
    )
    token = server.issue_token("stream-1", call_id="old-call")
    grant = server._consume_token("stream-1", token)

    assert grant is not None
    assert server._grant_validator("stream-1", grant) is False
    assert accepted == [("stream-1", "old-call")]


def test_audio_stream_server_uses_explicit_public_host_for_wildcard_bind():
    server = AudioStreamServer(AudioManager(), "0.0.0.0", 8765, "pi.local")
    token = server.issue_token("call1")

    assert server.public_host == "pi.local"
    assert server.stream_url("call1", token).startswith("ws://pi.local:8765/")


def test_audio_stream_server_tracks_single_attached_client():
    server = AudioStreamServer(AudioManager(), "127.0.0.1", 8765)

    assert server._attach_client("call1", 1) is True
    assert server.client_attached("call1") is True
    assert server._attach_client("call1", 2) is False
    server._detach_client("call1", 2)
    assert server.client_attached("call1") is True
    server._detach_client("call1", 1)
    assert server.client_attached("call1") is False


def test_audio_stream_server_reports_real_client_lifecycle_only_once():
    events = []
    server = AudioStreamServer(
        AudioManager(),
        "127.0.0.1",
        8765,
        on_client_connected=lambda stream_id: events.append(("up", stream_id)),
        on_client_disconnected=lambda stream_id: events.append(("down", stream_id)),
    )

    assert server._attach_client("call1", 1) is True
    server._detach_client("call1", 2)
    server._detach_client("call1", 1)
    server._detach_client("call1", 1)

    assert events == [("up", "call1"), ("down", "call1")]


def test_audio_stream_server_wildcard_bind_does_not_return_loopback(monkeypatch):
    monkeypatch.setattr(sidecar_mod.socket, "getfqdn", lambda: "hfp-pi.local")
    server = AudioStreamServer(AudioManager(), "0.0.0.0", 8765)

    assert server.public_host == "hfp-pi.local"
    assert not server.stream_url("call1", "token").startswith("ws://127.0.0.1:")
