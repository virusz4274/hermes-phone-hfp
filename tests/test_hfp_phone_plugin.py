"""Tests for the repo-local Hermes HFP phone platform helpers."""

import asyncio
import wave

from hermes_platforms.hfp_phone.adapter import (
    HFPPhoneAdapter,
    PCM_SAMPLE_RATE,
    _normalize_phone_target,
    _rms_s16le,
    _resolve_standalone_target,
    _split_csv,
    _standalone_send,
    _write_wav,
)
import hermes_platforms.hfp_phone.adapter as adapter_mod


def test_split_csv_trims_and_skips_empty_values():
    assert _split_csv(" a, b ,,c ") == {"a", "b", "c"}


def test_rms_s16le_detects_silence_and_signal():
    assert _rms_s16le(b"\x00\x00" * 10) == 0.0
    assert _rms_s16le((1000).to_bytes(2, "little", signed=True) * 10) == 1000.0


def test_write_wav_uses_hfp_audio_format(tmp_path):
    path = tmp_path / "sample.wav"
    _write_wav(path, b"\x00\x00" * 80)

    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == PCM_SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 80


def test_phone_target_normalization():
    assert _normalize_phone_target("tel:+1 (555) 123-4567") == "+15551234567"


def test_standalone_target_uses_owner_for_home_alias():
    assert (
        _resolve_standalone_target("hfp-phone", "+15551234567", "hfp-phone")
        == "+15551234567"
    )


def test_standalone_target_accepts_direct_number():
    assert (
        _resolve_standalone_target("+1 555 222 3333", "+15551234567", "hfp-phone")
        == "+15552223333"
    )


class _FakeClient:
    def __init__(self):
        self.calls = 0
        self.tool_calls = []

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        self.tool_calls.append((name, arguments or {}))
        return {"ok": True, "stream_url": "ws://example.test/audio"}


def _adapter_for_audio_tests():
    adapter = HFPPhoneAdapter.__new__(HFPPhoneAdapter)
    adapter._client = _FakeClient()
    adapter.session_id = "test-session"
    adapter._audio_task = None
    adapter._ws = None
    adapter.owner_number = "+15551234567"
    adapter.home_channel = "hfp-phone"
    adapter.call_timeout_seconds = 12.0
    adapter._running = True
    adapter.stream_runs = 0

    async def _run_audio_stream(_stream_url):
        adapter.stream_runs += 1
        await asyncio.sleep(60)

    adapter._run_audio_stream = _run_audio_stream
    return adapter


class _OpenWs:
    closed = False


async def test_ensure_outbound_call_dials_home_target(monkeypatch):
    adapter = _adapter_for_audio_tests()
    calls = []

    async def _call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
        return {"ok": True}

    async def _start_audio_stream():
        adapter._ws = _OpenWs()

    async def _status(_url):
        return {"call_state": "idle", "audio_active": False}

    monkeypatch.setattr(adapter_mod, "_read_json_url_async", _status)
    adapter._client.call_tool = _call_tool
    adapter.status_url = "http://status.test/status"
    adapter._start_audio_stream = _start_audio_stream

    await adapter._ensure_outbound_call_for_send("hfp-phone")

    assert calls == [
        (
            "dial_and_wait",
            {"number": "+15551234567", "timeout_seconds": 12.0},
        )
    ]


async def test_standalone_send_dials_streams_and_hangs_up(monkeypatch):
    calls = []

    class _Config:
        extra = {"auto_hangup_idle_seconds": "0.001"}

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "ensure_audio_stream":
                return {"ok": True, "stream_url": "ws://audio.test/call"}
            return {"ok": True}

    async def _send_pcm(_stream_url, _pcm):
        calls.append(("send_pcm", {}))

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)
    async def _synthesize(_text):
        return b"pcm"

    monkeypatch.setattr(adapter_mod, "_synthesize_pcm_for_call_async", _synthesize)
    monkeypatch.setattr(adapter_mod, "_send_pcm_to_stream_url", _send_pcm)

    result = await _standalone_send(
        _Config(),
        "+123",
        "Take your medicine",
    )

    assert result["success"] is True
    assert calls == [
        (
            "dial_and_wait",
            {"number": "+123", "timeout_seconds": 45.0},
        ),
        ("ensure_audio_stream", {"session_id": "active-call"}),
        ("send_pcm", {}),
        ("hangup", {}),
        ("stop_audio_capture", {"session_id": "active-call"}),
    ]


async def test_start_audio_stream_does_not_duplicate_running_task():
    adapter = _adapter_for_audio_tests()

    await adapter._start_audio_stream()
    await adapter._start_audio_stream()

    assert adapter._client.calls == 1
    assert adapter._audio_task is not None

    await adapter._stop_audio_stream()


async def test_stop_audio_stream_releases_mcp_audio_session():
    adapter = _adapter_for_audio_tests()

    await adapter._start_audio_stream()
    await adapter._stop_audio_stream()

    assert ("stop_audio_capture", {"session_id": "test-session"}) in getattr(adapter._client, "tool_calls")


async def test_completed_audio_task_is_cleared_for_restart():
    adapter = _adapter_for_audio_tests()
    done = asyncio.create_task(asyncio.sleep(0))
    await done

    adapter._audio_task = done
    adapter._audio_task_done(done)

    assert adapter._audio_task is None
