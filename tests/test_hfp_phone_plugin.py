"""Tests for the repo-local Hermes HFP phone platform helpers."""

import asyncio
import json
import subprocess
import sys
import types
import wave
from pathlib import Path

import pytest

from hermes_platforms.hfp_phone.adapter import (
    HFPCallNoLongerActive,
    HFPPhoneAdapter,
    PCM_SAMPLE_RATE,
    check_requirements,
    validate_config,
    register,
    _classify_caller_role,
    _env_enablement,
    _apply_yaml_config,
    _normalize_phone_target,
    _rms_s16le,
    _resolve_standalone_target,
    _strip_hfp_target_prefix,
    _synthesize_audio_file,
    _split_csv,
    _standalone_send,
    _transcribe_pcm,
    _write_wav,
)
import hermes_platforms.hfp_phone.adapter as adapter_mod


def test_split_csv_trims_and_skips_empty_values():
    assert _split_csv(" a, b ,,c ") == {"a", "b", "c"}


def test_hfp_adapter_enforces_own_access_policy():
    assert HFPPhoneAdapter.enforces_own_access_policy is True


def test_hfp_adapter_disables_message_edit_streaming():
    assert HFPPhoneAdapter.SUPPORTS_MESSAGE_EDITING is False


def test_classify_caller_role_prefers_admin_then_trusted():
    assert _classify_caller_role("22:22:D2:F8:01:7A", {"22:22:D2:F8:01:7A"}, set()) == "admin"
    assert _classify_caller_role("22:22:D2:F8:01:7A", set(), {"22:22:D2:F8:01:7A"}) == "trusted"
    assert _classify_caller_role("22:22:D2:F8:01:7A", set(), set()) == "unknown"


def test_classify_caller_role_normalizes_phone_number_aliases():
    assert _classify_caller_role("tel:123", {"123"}, set()) == "admin"


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


def test_standalone_target_accepts_hfp_prefixed_number():
    assert (
        _resolve_standalone_target("hfp_phone:+91 79076 86219", "", "hfp-phone")
        == "+917907686219"
    )
    assert _strip_hfp_target_prefix("hfp-phone:+123") == "+123"


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
    adapter.voice_mode = "classic"
    adapter._audio_task = None
    adapter._gemini_poll_task = None
    adapter._ws = None
    adapter.owner_number = "123"
    adapter.home_channel = "hfp-phone"
    adapter.admin_callers = {"123"}
    adapter.trusted_callers = set()
    adapter._active_caller_id = "unknown"
    adapter._active_caller_role = "unknown"
    adapter._active_call_id = None
    adapter.call_timeout_seconds = 12.0
    adapter.auto_hangup_idle_seconds = 0
    adapter._idle_hangup_task = None
    adapter._running = True
    adapter._gemini_active = False
    adapter._gemini_fallback_to_classic = False
    adapter._pending_gemini_request_ids = adapter_mod.deque()
    adapter.stream_runs = 0

    async def _run_audio_stream(_stream_url):
        adapter.stream_runs += 1
        await asyncio.sleep(60)

    adapter._run_audio_stream = _run_audio_stream
    return adapter


def test_hermes_config_requires_mcp_url(monkeypatch):
    monkeypatch.delenv("HFP_PHONE_MCP_URL", raising=False)
    monkeypatch.delenv("HFP_PHONE_STATUS_URL", raising=False)

    assert check_requirements() is False
    assert validate_config(None) is False


def test_hermes_config_accepts_explicit_mcp_url(monkeypatch):
    monkeypatch.setenv("HFP_PHONE_MCP_URL", "http://raspberrypi.local:8000/mcp")

    assert check_requirements() is True
    assert validate_config(None) is True


def test_env_enablement_disables_gateway_restart_notifications(monkeypatch):
    monkeypatch.setenv("HFP_PHONE_MCP_URL", "http://raspberrypi.local:8000/mcp")
    monkeypatch.setenv("HFP_PHONE_OWNER_NUMBER", "+155****4567")

    assert _env_enablement()["gateway_restart_notification"] is False


def test_apply_yaml_config_bridges_role_and_restart_settings():
    result = _apply_yaml_config(
        {"hfp_phone": {"admin_callers": "22:22:D2:F8:01:7A"}},
        {
            "trusted_callers": "AA:BB:CC:DD:EE:FF",
            "gateway_restart_notification": False,
            "unauthorized_dm_behavior": "ignore",
        },
    )

    assert result == {
        "admin_callers": "22:22:D2:F8:01:7A",
        "trusted_callers": "AA:BB:CC:DD:EE:FF",
        "gateway_restart_notification": False,
        "unauthorized_dm_behavior": "ignore",
    }


def test_hfp_mcp_package_does_not_import_hermes_modules():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(repo_root / 'src')!r}); "
                "import hfp_mcp; "
                "print(any(name.startswith('hermes') for name in sys.modules))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert proc.stdout.strip() == "False"


async def test_connect_fails_when_underlying_mcp_is_unavailable():
    adapter = HFPPhoneAdapter.__new__(HFPPhoneAdapter)
    adapter._client = _FakeClient()

    async def _call_tool(_name, _arguments=None):
        return {"ok": False, "error": "connection refused"}

    adapter._client.call_tool = _call_tool

    assert await adapter.connect() is False
    assert not getattr(adapter, "_running", False)


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
            {"number": "123", "timeout_seconds": 12.0},
        )
    ]


async def test_ensure_outbound_call_reuses_active_call_stream(monkeypatch):
    adapter = _adapter_for_audio_tests()
    calls = []

    async def _call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
        return {"ok": True}

    async def _start_audio_stream():
        adapter._ws = _OpenWs()

    async def _status(_url):
        return {"call_state": "active", "audio_active": True}

    monkeypatch.setattr(adapter_mod, "_read_json_url_async", _status)
    adapter._client.call_tool = _call_tool
    adapter.status_url = "http://status.test/status"
    adapter._start_audio_stream = _start_audio_stream

    await adapter._ensure_outbound_call_for_send("hfp-phone")

    assert calls == []


async def test_ensure_outbound_call_does_not_redial_active_call_without_audio(monkeypatch):
    adapter = _adapter_for_audio_tests()
    calls = []

    async def _call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
        return {"ok": True}

    async def _start_audio_stream():
        return None

    async def _status(_url):
        return {"call_state": "active", "audio_active": True}

    monkeypatch.setattr(adapter_mod, "_read_json_url_async", _status)
    adapter._client.call_tool = _call_tool
    adapter.status_url = "http://status.test/status"
    adapter._start_audio_stream = _start_audio_stream

    with pytest.raises(RuntimeError, match="Call audio stream"):
        await adapter._ensure_outbound_call_for_send("hfp-phone")

    assert calls == []


async def test_send_pcm_fails_when_audio_stream_is_not_connected():
    adapter = _adapter_for_audio_tests()
    adapter._ws = None

    with pytest.raises(HFPCallNoLongerActive, match="not connected"):
        await adapter._send_pcm(b"\x00\x00")


async def test_send_pcm_fails_cleanly_if_audio_stream_disappears_mid_send():
    adapter = _adapter_for_audio_tests()

    class _FakeWS:
        closed = False

        def __init__(self):
            self.frames = 0

        async def send_bytes(self, _frame):
            self.frames += 1
            adapter._ws = None

    ws = _FakeWS()
    adapter._ws = ws

    with pytest.raises(HFPCallNoLongerActive, match="closed while sending"):
        await adapter._send_pcm(b"\x00\x00" * 700)

    assert ws.frames == 1


async def test_send_drops_stale_inbound_reply_without_fallback_retry(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter.status_url = "http://status.test/status"

    class _FakeSendResult:
        def __init__(self, success, message_id=None, error=None, retryable=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.retryable = retryable

    async def _idle_status(_url):
        return {"call_state": "idle", "audio_active": False}

    async def _should_not_synthesize(_text):  # pragma: no cover - assertion helper
        raise AssertionError("stale inbound replies must not be synthesized")

    monkeypatch.setattr(adapter_mod, "SendResult", _FakeSendResult)
    monkeypatch.setattr(adapter_mod, "_read_json_url_async", _idle_status)
    monkeypatch.setattr(adapter_mod, "_synthesize_pcm_for_call_async", _should_not_synthesize)

    result = await adapter.send("hfp-phone:22:22:D2:F8:01:7A", "hello after hangup")

    assert result.success is True
    assert adapter._client.tool_calls == []


def test_hfp_event_includes_caller_role_metadata(monkeypatch):
    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    class _FakeSessionSource:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeMessageType:
        TEXT = "text"

    class _FakeMessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(adapter_mod, "Platform", _FakePlatform)
    monkeypatch.setattr(adapter_mod, "SessionSource", _FakeSessionSource)
    monkeypatch.setattr(adapter_mod, "MessageType", _FakeMessageType)
    monkeypatch.setattr(adapter_mod, "MessageEvent", _FakeMessageEvent)

    adapter = _adapter_for_audio_tests()
    adapter._active_chat_id = "hfp-phone:22:22:D2:F8:01:7A"
    adapter._active_caller_id = "22:22:D2:F8:01:7A"
    adapter._active_caller_role = "trusted"
    adapter._active_call_id = "call-1"

    event = adapter._event("hello")

    assert event.raw_message["source"] == "hfp-phone"
    assert event.raw_message["hfp_caller_id"] == "22:22:D2:F8:01:7A"
    assert event.raw_message["hfp_role"] == "trusted"
    assert event.raw_message["hfp_call_id"] == "call-1"


async def test_handle_status_uses_one_hermes_session_per_call(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter._last_state = "idle"
    adapter._stop_audio_stream = lambda: asyncio.sleep(0)
    adapter._cancel_idle_hangup = lambda: None
    adapter._start_audio_stream = lambda: asyncio.sleep(0)

    await adapter._handle_status(
        {
            "connection": "connected",
            "connected_address": "22:22:D2:F8:01:7A",
            "call_state": "active",
            "audio_active": True,
        }
    )
    first_chat = adapter._active_chat_id
    first_call = adapter._active_call_id

    await adapter._handle_status(
        {
            "connection": "connected",
            "connected_address": "22:22:D2:F8:01:7A",
            "call_state": "active",
            "audio_active": True,
        }
    )
    assert adapter._active_chat_id == first_chat
    assert adapter._active_call_id == first_call

    await adapter._handle_status(
        {
            "connection": "connected",
            "connected_address": "22:22:D2:F8:01:7A",
            "call_state": "idle",
            "audio_active": False,
        }
    )
    assert adapter._active_call_id is None

    await adapter._handle_status(
        {
            "connection": "connected",
            "connected_address": "22:22:D2:F8:01:7A",
            "call_state": "active",
            "audio_active": True,
        }
    )
    assert adapter._active_chat_id != first_chat


async def test_standalone_send_dials_mcp_file_playback_and_hangs_up(monkeypatch, tmp_path):
    calls = []
    audio_path = tmp_path / "tts.mp3"
    audio_path.write_bytes(b"audio")

    class _Config:
        extra = {"auto_hangup_idle_seconds": "0.001"}

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "get_call_status":
                return {"call_state": "idle", "audio_active": False}
            return {"ok": True}

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)
    monkeypatch.setattr(adapter_mod, "_synthesize_audio_file", lambda _text: audio_path)

    result = await _standalone_send(
        _Config(),
        "+123",
        "Take your medicine",
    )

    assert result["success"] is True
    assert calls[0] == ("get_call_status", {})
    assert calls[1] == (
        "dial_and_wait",
        {"number": "+123", "timeout_seconds": 45.0},
    )
    assert calls[2][0] == "play_audio_file"
    assert calls[2][1]["audio_file"] == str(audio_path)
    assert calls[2][1]["session_id"].startswith("hfp-call-")
    assert calls[3] == ("hangup", {})
    assert calls[4] == (
        "stop_audio_capture",
        {"session_id": calls[2][1]["session_id"]},
    )


async def test_standalone_send_failure_after_dial_hangs_up_and_cleans(monkeypatch, tmp_path):
    calls = []
    audio_path = tmp_path / "tts.mp3"
    audio_path.write_bytes(b"audio")

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "get_call_status":
                return {"call_state": "idle", "audio_active": False}
            if name == "play_audio_file":
                return {"ok": False, "error": "audio sidecar rejected token"}
            return {"ok": True}

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)
    monkeypatch.setattr(adapter_mod, "_synthesize_audio_file", lambda _text: audio_path)

    result = await _standalone_send(None, "+123", "hello")

    assert result == {"error": "audio sidecar rejected token"}
    session_id = next(
        args["session_id"] for name, args in calls if name == "play_audio_file"
    )
    assert ("hangup", {}) in calls
    assert ("cleanup_audio_sessions", {}) in calls
    assert ("stop_audio_capture", {"session_id": session_id}) in calls


async def test_standalone_send_failure_on_active_call_does_not_hang_up(monkeypatch, tmp_path):
    calls = []
    audio_path = tmp_path / "tts.mp3"
    audio_path.write_bytes(b"audio")

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "get_call_status":
                return {"call_state": "active", "audio_active": True}
            if name == "play_audio_file":
                return {"ok": False, "error": "audio sidecar rejected token"}
            return {"ok": True}

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)
    monkeypatch.setattr(adapter_mod, "_synthesize_audio_file", lambda _text: audio_path)

    result = await _standalone_send(None, "+123", "hello")

    assert result == {"error": "audio sidecar rejected token"}
    assert ("hangup", {}) not in calls
    assert ("cleanup_audio_sessions", {}) not in calls
    session_id = next(
        args["session_id"] for name, args in calls if name == "play_audio_file"
    )
    assert ("stop_audio_capture", {"session_id": session_id}) in calls


async def test_standalone_send_audio_file_skips_tts(monkeypatch, tmp_path):
    calls = []
    audio_path = tmp_path / "message.mp3"
    audio_path.write_bytes(b"audio")

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "get_call_status":
                return {"call_state": "active", "audio_active": True}
            return {"ok": True}

    def _should_not_tts(_text):  # pragma: no cover - assertion helper
        raise AssertionError("audio_file should skip TTS")

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)
    monkeypatch.setattr(adapter_mod, "_synthesize_audio_file", _should_not_tts)

    result = await _standalone_send(None, "+123", "", audio_file=str(audio_path))

    assert result["success"] is True
    assert ("dial_and_wait", {"number": "+123", "timeout_seconds": 45.0}) not in calls
    assert calls[1][0] == "play_audio_file"
    assert calls[1][1]["audio_file"] == str(audio_path)


async def test_standalone_send_audio_file_can_be_mcp_local(monkeypatch):
    calls = []

    class _FakeStandaloneClient:
        def __init__(self, _mcp_url):
            pass

        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "get_call_status":
                return {"call_state": "active", "audio_active": True}
            return {"ok": True}

    monkeypatch.setattr(adapter_mod, "HFPControlClient", _FakeStandaloneClient)

    result = await _standalone_send(
        None,
        "+123",
        "",
        audio_file="/mnt/hfp-server/message.mp3",
    )

    assert result["success"] is True
    assert calls[1] == (
        "play_audio_file",
        {
            "audio_file": "/mnt/hfp-server/message.mp3",
            "session_id": calls[1][1]["session_id"],
            "tail_ms": 1000.0,
        },
    )


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


async def test_gemini_voice_mode_send_does_not_use_hermes_tts(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter.voice_mode = "gemini_live"
    adapter.status_url = "http://status.test/status"
    calls = []

    class _FakeSendResult:
        def __init__(self, success, message_id=None, error=None, retryable=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.retryable = retryable

    async def _status(_url):
        return {"call_state": "active", "audio_active": True}

    async def _call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
        return {"ok": True}

    async def _should_not_tts(_text):  # pragma: no cover - assertion helper
        raise AssertionError("Gemini mode must not call Hermes TTS")

    monkeypatch.setattr(adapter_mod, "SendResult", _FakeSendResult)
    monkeypatch.setattr(adapter_mod, "_read_json_url_async", _status)
    monkeypatch.setattr(adapter_mod, "_synthesize_pcm_for_call_async", _should_not_tts)
    adapter._client.call_tool = _call_tool

    result = await adapter.send("hfp-phone:caller:call", "please continue")

    assert result.success is True
    assert calls[0][0] == "start_gemini_live_call"
    assert calls[1] == (
        "send_gemini_live_text",
        {
            "text": "please continue",
            "urgency": "normal",
            "speak_to_caller": True,
        },
    )


async def test_gemini_voice_mode_send_submits_pending_request(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter.voice_mode = "gemini_live"
    adapter._gemini_active = True
    adapter._pending_gemini_request_ids.append("req-1")
    calls = []

    class _FakeSendResult:
        def __init__(self, success, message_id=None, error=None, retryable=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.retryable = retryable

    async def _call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
        return {"ok": True}

    monkeypatch.setattr(adapter_mod, "SendResult", _FakeSendResult)
    adapter._client.call_tool = _call_tool

    result = await adapter.send("hfp-phone:caller:call", "done")

    assert result.success is True
    assert calls == [
        (
            "submit_gemini_live_result",
            {
                "request_id": "req-1",
                "result": "done",
                "speak_to_caller": True,
            },
        )
    ]


async def test_auto_voice_mode_falls_back_to_classic_when_gemini_unavailable(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter.voice_mode = "auto"
    started_classic = []

    async def _call_tool(name, arguments=None):
        if name == "start_gemini_live_call":
            return {"ok": False, "error": "missing_api_key"}
        return {"ok": True}

    async def _start_audio_stream():
        started_classic.append(True)

    adapter._client.call_tool = _call_tool
    adapter._start_audio_stream = _start_audio_stream

    await adapter._start_call_voice()

    assert started_classic == [True]
    assert adapter._gemini_fallback_to_classic is True


async def test_gemini_request_poll_dispatches_to_hermes(monkeypatch):
    adapter = _adapter_for_audio_tests()
    adapter.voice_mode = "gemini_live"
    adapter._gemini_active = True
    handled = []

    async def _call_tool(name, arguments=None):
        adapter._gemini_active = False
        return {
            "ok": True,
            "requests": [
                {
                    "request_id": "req-1",
                    "name": "ask_hermes",
                    "arguments": {"task": "set a reminder", "context": "caller asked"},
                }
            ],
        }

    async def _handle_message(event):
        handled.append(event.text)

    monkeypatch.setattr(adapter, "handle_message", _handle_message, raising=False)
    adapter._event = lambda text: types.SimpleNamespace(text=text)
    adapter._client.call_tool = _call_tool

    await adapter._poll_gemini_live_requests()

    assert adapter._pending_gemini_request_ids.popleft() == "req-1"
    assert handled == ["Gemini Live asks Hermes: set a reminder\nContext: caller asked"]


def _install_fake_tools(monkeypatch, *, tts_module=None, transcription_module=None):
    tools_pkg = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    if tts_module is not None:
        monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_module)
    if transcription_module is not None:
        monkeypatch.setitem(sys.modules, "tools.transcription_tools", transcription_module)


def test_synthesize_audio_file_uses_current_hermes_tts_callable(monkeypatch, tmp_path):
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"not-real-audio")
    calls = []
    tts_module = types.ModuleType("tools.tts_tool")

    def text_to_speech_tool(*, text):
        calls.append(text)
        return json.dumps({"success": True, "file_path": str(audio_path)})

    tts_module.text_to_speech_tool = text_to_speech_tool
    _install_fake_tools(monkeypatch, tts_module=tts_module)

    assert _synthesize_audio_file("hello") == audio_path
    assert calls == ["hello"]


def test_synthesize_audio_file_rejects_legacy_tts_callable(monkeypatch):
    tts_module = types.ModuleType("tools.tts_tool")
    tts_module.text_to_speech = lambda *, text: {"success": True, "file_path": "/tmp/old.mp3"}
    _install_fake_tools(monkeypatch, tts_module=tts_module)

    with pytest.raises(RuntimeError, match="text_to_speech_tool"):
        _synthesize_audio_file("hello")


def test_transcribe_pcm_uses_current_hermes_stt_callable(monkeypatch):
    calls = []
    transcription_module = types.ModuleType("tools.transcription_tools")

    def transcribe_audio(path):
        calls.append(path)
        with wave.open(path, "rb") as wav:
            assert wav.getframerate() == PCM_SAMPLE_RATE
            assert wav.getnchannels() == 1
        return {"success": True, "transcript": "  hello from phone  "}

    transcription_module.transcribe_audio = transcribe_audio
    _install_fake_tools(monkeypatch, transcription_module=transcription_module)

    assert _transcribe_pcm(b"\x00\x00" * 80) == "hello from phone"
    assert len(calls) == 1


async def test_hfp_phone_call_tool_accepts_dispatcher_kwargs(monkeypatch):
    calls = []

    async def _send(_pconfig, chat_id, message, **kwargs):
        calls.append((chat_id, message, kwargs))
        return {"success": True, "message_id": "msg-1"}

    monkeypatch.setattr(adapter_mod, "_standalone_send", _send)

    result = await adapter_mod._hfp_phone_call_tool(
        {"number": "+123", "message": "hello"}, task_id="task-1"
    )

    assert result == {"ok": True, "message_id": "msg-1"}
    assert calls[0][0] == "+123"
    assert calls[0][1] == "hello"


def test_hfp_phone_call_tool_is_registered_as_async():
    calls = []

    class _Ctx:
        def register_platform(self, **kwargs):
            calls.append(("platform", kwargs))

        def register_tool(self, **kwargs):
            calls.append(("tool", kwargs))

    register(_Ctx())

    tool = next(kwargs for kind, kwargs in calls if kind == "tool" and kwargs["name"] == "hfp_phone_call")
    assert tool["is_async"] is True
