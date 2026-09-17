"""Tests for MCP-level SCO audio cleanup behavior."""

import subprocess

from hfp_mcp import server
from hfp_mcp.audio.sco import AudioManager, SCOHealthEvent
from hfp_mcp.media import MediaLeaseManager
from hfp_mcp.state import CallState, ConnectionState, HFPState


def _fresh_server_state(monkeypatch):
    state = HFPState()
    manager = AudioManager()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_audio_manager", manager)
    return state, manager


def test_stale_connected_health_event_does_not_resurrect_ended_audio(monkeypatch):
    state, _manager = _fresh_server_state(monkeypatch)
    monkeypatch.setattr(server, "_media_leases", MediaLeaseManager())
    monkeypatch.setattr(server, "_audio_stream_server", None)

    server._on_sco_health(
        SCOHealthEvent("connected", "AA:BB:CC:DD:EE:FF", 1)
    )

    assert state.sco_connected is False
    assert state.audio_active is False


async def test_cleanup_audio_sessions_stops_all_sessions(monkeypatch):
    state, manager = _fresh_server_state(monkeypatch)
    manager.create_session("one", "AA:BB:CC:DD:EE:FF")
    manager.create_session("two", "AA:BB:CC:DD:EE:FF")
    state.set_sco_connected(True)

    result = await server.cleanup_audio_sessions()

    assert result == {"ok": True, "sessions_stopped": 2}
    assert manager.session_count() == 0
    assert state.sco_connected is False


async def test_get_phone_context_recommends_cleanup_for_stale_sco(monkeypatch):
    state, _manager = _fresh_server_state(monkeypatch)
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.IDLE
    state.set_sco_connected(True)

    context = await server.get_phone_context()

    assert context["recommended_next_action"] == "cleanup_audio_sessions"
    assert context["ready_to_call"] is False
    assert context["call_state"] == "idle"
    assert context["sco_connected"] is True


async def test_stop_audio_capture_reports_physical_transport_not_logical_sessions(monkeypatch):
    state, manager = _fresh_server_state(monkeypatch)
    manager.create_session("one", "AA:BB:CC:DD:EE:FF")
    manager.create_session("two", "AA:BB:CC:DD:EE:FF")
    state.set_sco_connected(True)

    first = await server.stop_audio_capture("one")

    assert first == {"ok": True}
    assert manager.session_count() == 1
    # These registry-only test sessions were never started, so no physical SCO
    # transport remains ready even though one logical handle still exists.
    assert state.sco_connected is False

    second = await server.stop_audio_capture("two")

    assert second == {"ok": True}
    assert manager.session_count() == 0
    assert state.sco_connected is False


async def test_clear_audio_playback_clears_session_queue(monkeypatch):
    _state, manager = _fresh_server_state(monkeypatch)
    session = manager.create_session("one", "AA:BB:CC:DD:EE:FF")
    with session._pb_lock:
        session._playback.extend(b"abcdef")

    result = await server.clear_audio_playback("one")

    assert result == {"ok": True, "session_id": "one", "bytes_cleared": 6}
    assert session._take_playback(1) == b"\x00"


def test_convert_audio_file_to_pcm_uses_hfp_format(monkeypatch, tmp_path):
    audio_path = tmp_path / "message.mp3"
    audio_path.write_bytes(b"audio")
    captured = {}

    class _Proc:
        stdout = b"pcm"

    def _run(cmd, *, check, capture_output, timeout):
        captured["cmd"] = cmd
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        return _Proc()

    monkeypatch.setattr(server.subprocess, "run", _run)

    assert server._convert_audio_file_to_pcm(audio_path) == b"pcm"
    cmd = captured["cmd"]
    assert cmd[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i"]
    assert cmd[cmd.index("-f") + 1] == "s16le"
    assert cmd[cmd.index("-acodec") + 1] == "pcm_s16le"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "8000"
    assert captured["check"] is True
    assert captured["capture_output"] is True
    assert captured["timeout"] == 60


async def test_play_audio_file_converts_and_queues_tail(monkeypatch, tmp_path):
    audio_path = tmp_path / "message.mp3"
    audio_path.write_bytes(b"audio")
    queued = {}
    session = object()

    async def _ensure_audio_session(session_id):
        queued["session_id"] = session_id
        return {"ok": True, "session": session, "session_reused": True}

    async def _queue_pcm_realtime(actual_session, pcm):
        queued["session"] = actual_session
        queued["pcm"] = pcm
        return len(pcm)

    monkeypatch.setattr(server, "_convert_audio_file_to_pcm", lambda _path: b"\x01\x02")
    monkeypatch.setattr(server, "_ensure_audio_session", _ensure_audio_session)
    monkeypatch.setattr(server, "_queue_pcm_realtime", _queue_pcm_realtime)

    result = await server.play_audio_file(str(audio_path), "one-shot", tail_ms=40)

    assert result["ok"] is True
    assert result["session_id"] == "one-shot"
    assert result["session_reused"] is True
    assert result["bytes_pcm"] == 2
    assert result["bytes_queued"] == 642
    assert queued["session"] is session
    assert queued["pcm"] == b"\x01\x02" + (b"\x00" * 640)


async def test_queue_pcm_realtime_uses_twenty_millisecond_pacing(monkeypatch):
    frames = []
    sleeps = []

    class _Session:
        def queue_playback(self, frame):
            frames.append(bytes(frame))

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(server.asyncio, "sleep", _sleep)

    queued = await server._queue_pcm_realtime(
        _Session(), b"x" * (server.STREAM_FRAME_BYTES * 2)
    )

    assert queued == server.STREAM_FRAME_BYTES * 2
    assert [len(frame) for frame in frames] == [
        server.STREAM_FRAME_BYTES,
        server.STREAM_FRAME_BYTES,
    ]
    assert sleeps == [0.02, 0.02]


async def test_play_audio_file_reports_missing_file(tmp_path):
    result = await server.play_audio_file(str(tmp_path / "missing.mp3"))

    assert result["ok"] is False
    assert "not found" in result["error"]


async def test_play_audio_file_reports_ffmpeg_failure(monkeypatch, tmp_path):
    audio_path = tmp_path / "message.mp3"
    audio_path.write_bytes(b"audio")

    def _convert(_path):
        raise subprocess.CalledProcessError(
            1,
            ["ffmpeg"],
            stderr=b"invalid audio",
        )

    monkeypatch.setattr(server, "_convert_audio_file_to_pcm", _convert)

    result = await server.play_audio_file(str(audio_path))

    assert result["ok"] is False
    assert "invalid audio" in result["error"]


async def test_dial_and_play_audio_file_reuses_active_call(monkeypatch):
    state, _manager = _fresh_server_state(monkeypatch)
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.ACTIVE
    state.audio_active = True
    calls = []

    async def _dial_and_wait(_number, _timeout):
        raise AssertionError("active calls must not be redialed")

    async def _play(audio_file, session_id, tail_ms):
        calls.append((audio_file, session_id, tail_ms))
        return {"ok": True, "session_id": session_id, "bytes_queued": 1}

    monkeypatch.setattr(server, "dial_and_wait", _dial_and_wait)
    monkeypatch.setattr(server, "play_audio_file", _play)

    result = await server.dial_and_play_audio_file(
        "+123",
        "/tmp/message.mp3",
        session_id="active-call",
        tail_ms=20,
    )

    assert result["ok"] is True
    assert result["dialed"] is False
    assert calls == [("/tmp/message.mp3", "active-call", 20)]
