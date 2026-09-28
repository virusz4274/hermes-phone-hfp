import json
import asyncio
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from hfp_mcp import gemini_live


def _server_tool_names(env: dict | None = None, extra_path: Path | None = None) -> set[str]:
    repo_root = Path(__file__).resolve().parents[1]
    pythonpath = [str(repo_root / "src")]
    if extra_path is not None:
        pythonpath.insert(0, str(extra_path))
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from hfp_mcp import server; "
                "print(json.dumps(sorted(server.mcp._tool_manager._tools)))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **(env or {}),
            "PYTHONPATH": ":".join(pythonpath),
            "PATH": "/usr/bin:/bin",
        },
    )
    return set(json.loads(proc.stdout))


def _fake_gemini_deps(tmp_path: Path) -> None:
    google = tmp_path / "google"
    google.mkdir()
    (google / "__init__.py").write_text("")
    genai = google / "genai"
    genai.mkdir()
    (genai / "__init__.py").write_text(
        textwrap.dedent(
            """
            class Client:
                def __init__(self, *args, **kwargs):
                    pass
            """
        )
    )
    (genai / "live.py").write_text(
        textwrap.dedent(
            """
            class AsyncSession:
                async def send_realtime_input(self, **kwargs): pass
                async def send_tool_response(self, **kwargs): pass
                async def receive(self): pass
                async def close(self): pass
            class AsyncLive:
                def connect(self, **kwargs): pass
            """
        )
    )
    (genai / "types.py").write_text(
        textwrap.dedent(
            """
            class _Type:
                def __init__(self, *args, **kwargs): pass
            LiveConnectConfig = _Type
            FunctionResponse = _Type
            Blob = _Type
            """
        )
    )
    (tmp_path / "aiohttp.py").write_text("")
    (tmp_path / "soxr.py").write_text("")
    (tmp_path / "numpy.py").write_text("")


def test_gemini_live_availability_disabled(monkeypatch):
    monkeypatch.delenv("HFP_GEMINI_LIVE_ENABLED", raising=False)

    assert gemini_live.availability()["reason"] == "disabled"


def test_gemini_tools_absent_when_disabled():
    tools = _server_tool_names({})

    assert "start_gemini_live_call" not in tools
    assert "get_gemini_live_status" not in tools
    assert {
        "get_capabilities",
        "start_live_ai_call",
        "get_live_ai_status",
        "send_live_instruction",
        "speak_to_caller",
        "poll_live_ai_requests",
        "get_call_transcript",
    } <= tools


def test_gemini_tools_absent_without_api_key():
    tools = _server_tool_names({"HFP_GEMINI_LIVE_ENABLED": "true"})

    assert "start_gemini_live_call" not in tools


def test_gemini_tools_present_when_enabled_configured_and_dependencies_exist(tmp_path):
    _fake_gemini_deps(tmp_path)

    tools = _server_tool_names(
        {
            "HFP_GEMINI_LIVE_ENABLED": "true",
            "HFP_GEMINI_API_KEY": "test-key",
        },
        tmp_path,
    )

    assert {
        "start_live_ai_call",
        "stop_live_ai_call",
        "get_live_ai_status",
        "send_live_ai_text",
        "poll_live_ai_requests",
        "get_live_ai_pending_requests",
        "submit_live_ai_result",
        "cancel_live_request",
        "clear_live_requests",
        "get_call_transcript",
        "get_last_call_summary",
        "start_gemini_live_call",
        "stop_gemini_live_call",
        "get_gemini_live_status",
        "send_gemini_live_text",
        "poll_gemini_live_requests",
        "get_gemini_live_pending_requests",
        "submit_gemini_live_result",
    } <= tools


async def test_pending_gemini_requests_remain_visible_after_poll():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    request = gemini_live.GeminiRequest(
        request_id="req-1",
        function_call_id="fc-1",
        name="ask_mcp_client",
        arguments={"task": "check status"},
        created_at=123.0,
    )
    await manager._requests.put(request)

    first = await manager.poll_requests(timeout_seconds=0.01)
    second = await manager.poll_requests(timeout_seconds=0.01)
    pending = manager.pending_requests()

    assert first["requests"] == [request.to_dict()]
    assert second["requests"] == []
    assert pending["requests"] == [request.to_dict()]
    assert manager.status()["pending_requests"] == 1
    assert manager.status()["queued_requests"] == 0
    assert manager.status()["total_unresolved_requests"] == 1


def test_gemini_function_declarations_expose_hermes_broker_tools():
    declarations = gemini_live._function_declarations()
    names = {item["name"] for item in declarations}

    assert {
        "ask_hermes",
        "notify_hermes",
        "get_hermes_context",
        "handoff_to_hermes",
    } <= names
    assert not ({"ask_mcp_client", "notify_mcp_client"} & names)
    ask_hermes = next(item for item in declarations if item["name"] == "ask_hermes")
    assert "explicit Hermes requests" in ask_hermes["description"]
    assert "external tools" in ask_hermes["description"]


def test_gemini_function_declarations_can_be_removed_by_role_policy():
    assert gemini_live._function_declarations(set()) == []
    assert [
        item["name"]
        for item in gemini_live._function_declarations({"notify_hermes"})
    ] == ["notify_hermes"]


def test_gemini_manager_accepts_role_filtered_tools_before_start():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )

    assert manager.configure_allowed_tools(set()) == {"ok": True, "allowed_tools": []}
    assert manager.status()["allowed_tools"] == []
    assert manager.status()["active_allowed_tools"] == []
    assert manager.configure_allowed_tools({"shell"})["error"] == "unsupported_live_ai_tool"


def test_pcm_resampler_changes_sample_rate_size():
    pytest.importorskip("soxr")
    pytest.importorskip("numpy")
    resampler = gemini_live.PcmResampler(8000, 16000)

    converted = b"".join(
        resampler.convert(b"\x00\x00" * 320) for _ in range(8)
    )
    converted += resampler.convert(b"", final=True)

    assert 10000 <= len(converted) <= 10500


def test_pcm_resampler_preserves_odd_partial_sample_bytes():
    pytest.importorskip("numpy")
    resampler = gemini_live.PcmResampler(8000, 8000)

    assert resampler.convert(b"\x01") == b""
    assert resampler.convert(b"\x02\x03") == b"\x01\x02"
    assert resampler.convert(b"", final=True) == b""


def test_pcm_resampler_attenuates_audio_above_hfp_nyquist():
    pytest.importorskip("soxr")
    np = pytest.importorskip("numpy")

    def rms_after_downsample(frequency_hz):
        time_axis = np.arange(12000) / gemini_live.GEMINI_RECEIVE_RATE
        source = (
            np.sin(2 * np.pi * frequency_hz * time_axis) * 20000
        ).astype("<i2")
        resampler = gemini_live.PcmResampler(
            gemini_live.GEMINI_RECEIVE_RATE, gemini_live.HFP_RATE
        )
        output = bytearray()
        raw = source.tobytes()
        for offset in range(0, len(raw), 1920):
            output.extend(resampler.convert(raw[offset : offset + 1920]))
        output.extend(resampler.convert(b"", final=True))
        samples = np.frombuffer(output, dtype="<i2").astype(float)
        samples = samples[100:-100]
        return float(np.sqrt(np.mean(samples * samples)))

    audible = rms_after_downsample(1000)
    aliased = rms_after_downsample(6000)

    assert aliased < audible * 0.01


def test_audio_dependencies_are_warmed_once_per_process(monkeypatch):
    constructions = []

    class FakeResampler:
        def __init__(self, source_rate, destination_rate):
            constructions.append((source_rate, destination_rate))

        def convert(self, _pcm, *, final=False):
            return b""

    monkeypatch.setattr(gemini_live, "_audio_warmed", False)
    monkeypatch.setattr(gemini_live, "PcmResampler", FakeResampler)

    gemini_live._warm_audio_dependencies()
    gemini_live._warm_audio_dependencies()

    assert constructions == [
        (gemini_live.HFP_RATE, gemini_live.GEMINI_SEND_RATE),
        (gemini_live.GEMINI_RECEIVE_RATE, gemini_live.HFP_RATE),
    ]


def test_gemini_live_config_enables_transcription_resumption_and_compression():
    config = gemini_live._live_config("caller context", "resume-handle", {"ask_hermes"})

    assert config["input_audio_transcription"] == {}
    assert config["output_audio_transcription"] == {}
    assert config["session_resumption"] == {"handle": "resume-handle"}
    assert config["context_window_compression"]["sliding_window"]
    assert config["tools"][0]["function_declarations"][0]["name"] == "ask_hermes"


def test_gemini_live_config_explicitly_follows_the_callers_language():
    instruction = " ".join(
        gemini_live._live_config("", None, set())["system_instruction"].split()
    ).casefold()

    assert "detect the caller's spoken language" in instruction
    assert "reply in that same language" in instruction
    for language in ("malayalam", "hindi", "tamil", "english"):
        assert language in instruction
    assert "default to english when unclear" in instruction
    assert "clear speech or request, not one uncertain word" in instruction


def test_structured_tool_outcome_preserves_safer_speech_suppression():
    result = gemini_live._structured_tool_outcome(
        json.dumps(
            {
                "status": "ok",
                "message": "private result",
                "speak_to_caller": False,
            }
        ),
        speak_to_caller=True,
    )

    assert result["status"] == "ok"
    assert result["speak_to_caller"] is False


def test_structured_tool_outcome_never_truthifies_string_false():
    result = gemini_live._structured_tool_outcome(
        {
            "status": "ok",
            "message": "internal only",
            "speak_to_caller": "false",
        },
        speak_to_caller=True,
    )

    assert result["status"] == "ok"
    assert result["speak_to_caller"] is False
    assert result["contract_error"] == "invalid_speak_to_caller"


def test_module_availability_handles_broken_namespace(monkeypatch):
    def broken_find_spec(_name):
        raise ModuleNotFoundError("google")

    monkeypatch.setattr(gemini_live.importlib.util, "find_spec", broken_find_spec)

    assert gemini_live._module_available("google.genai") is False


async def test_gemini_tool_call_uses_exact_function_id_and_cancellation():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    manager._session_id = "call-1"
    function_call = types.SimpleNamespace(
        id="gemini-fc-17",
        name="ask_hermes",
        args={"task": "check status"},
    )
    response = types.SimpleNamespace(
        tool_call=types.SimpleNamespace(function_calls=[function_call])
    )
    await manager._handle_tool_calls(response)
    queued = await manager.poll_requests(0.01)

    assert queued["requests"][0]["request_id"] == "gemini-fc-17"
    assert queued["requests"][0]["function_call_id"] == "gemini-fc-17"

    cancellation = types.SimpleNamespace(
        tool_call_cancellation=types.SimpleNamespace(ids=["gemini-fc-17"])
    )
    await manager._handle_tool_cancellations(cancellation)
    assert manager.stale_requests()["requests"][0]["state"] == "cancelled"
    bridge = manager.status()["tool_bridge"]
    assert bridge["calls_received"] == 1
    assert bridge["calls_accepted"] == 1
    assert bridge["results_submitted"] == 0
    assert bridge["responses_sent"] == 0
    assert bridge["calls_cancelled"] == 1
    assert bridge["last_function"] == "ask_hermes"
    assert bridge["last_function_call_id"] == "gemini-fc-17"
    assert bridge["last_state"] == "cancelled"


async def test_gemini_interruption_clears_bounded_playback_queue():
    clears = []

    async def ok(*args):
        clears.append(args)
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    manager._session_id = "call-1"
    manager._stream_info = {"stream_id": "gemini-stream-1"}
    frame_count = 50  # Deliberately exceeds the former lossy 400 ms reservoir.
    for _ in range(frame_count):
        manager._enqueue_playback_pcm(b"\x00" * gemini_live.HFP_FRAME_BYTES)

    assert manager._playback_queue.qsize() == frame_count
    assert manager.status()["dropped_playback_frames"] == 0

    await manager._handle_interruption()

    assert manager._playback_queue.empty()
    assert manager.status()["playback_frames_interrupted"] == frame_count
    assert clears == [("gemini-stream-1",)]


def test_gemini_playback_preserves_large_burst_in_exact_order():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    # The failed hardware call lost 685 frames. Preserve a larger single SDK
    # burst so this regression cannot return behind a superficially bigger
    # but still inadequate queue.
    frames = [
        bytes([index % 251]) * gemini_live.HFP_FRAME_BYTES
        for index in range(700)
    ]

    manager._enqueue_playback_pcm(b"".join(frames))

    queued = [manager._playback_queue.get_nowait().pcm for _ in frames]
    assert queued == frames
    status = manager.status()
    assert status["dropped_playback_frames"] == 0
    assert status["playback_frames_enqueued"] == len(frames)
    assert status["playback_queue_peak_frames"] == len(frames)


def test_gemini_playback_overflow_fails_without_evicting_existing_speech():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._playback_queue = asyncio.Queue(maxsize=2)
    first = b"a" * gemini_live.HFP_FRAME_BYTES
    second = b"b" * gemini_live.HFP_FRAME_BYTES
    manager._enqueue_playback_pcm(first + second)

    with pytest.raises(
        gemini_live._PlaybackBufferOverflow,
        match="gemini_playback_backlog_exceeded",
    ):
        manager._enqueue_playback_pcm(b"c" * gemini_live.HFP_FRAME_BYTES)

    assert [manager._playback_queue.get_nowait().pcm for _ in range(2)] == [
        first,
        second,
    ]
    assert manager.status()["playback_overflow_events"] == 1


async def test_gemini_supervisor_reconnects_goaway_with_resumption_handle():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    configs = []

    class FakeSession:
        def __init__(self, generation):
            self.generation = generation
            self.receive_calls = 0

        async def receive(self):
            if self.generation == 1:
                self.receive_calls += 1
                if self.receive_calls > 1:
                    raise RuntimeError("first websocket closed")
                yield types.SimpleNamespace(
                    session_resumption_update=types.SimpleNamespace(
                        resumable=True, new_handle="resume-17"
                    ),
                    tool_call_cancellation=None,
                    tool_call=None,
                    go_away=types.SimpleNamespace(time_left="1s"),
                    server_content=None,
                )
            else:
                manager._stop_event.set()
                if False:
                    yield None

    class ConnectContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return None

    class FakeLive:
        def connect(self, *, model, config):
            configs.append((model, config))
            return ConnectContext(FakeSession(len(configs)))

    client = types.SimpleNamespace(aio=types.SimpleNamespace(live=FakeLive()))

    async def blocked_sender(_session):
        await manager._stop_event.wait()

    manager._gemini_input_sender = blocked_sender
    await manager._session_supervisor(client, "context")

    assert len(configs) == 2
    assert configs[1][1]["session_resumption"] == {"handle": "resume-17"}
    assert manager.status()["reconnect_count"] == 1


async def test_gemini_goaway_after_non_resumable_update_reconnects_cold():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    manager._session_id = "call-1"
    configs = []
    scope_at_connect = []

    class FakeSession:
        def __init__(self, generation):
            self.generation = generation

        async def receive(self):
            if self.generation == 1:
                yield types.SimpleNamespace(
                    session_resumption_update=types.SimpleNamespace(
                        resumable=True, new_handle="resume-before-tool"
                    ),
                    tool_call=types.SimpleNamespace(
                        function_calls=[
                            types.SimpleNamespace(
                                id="fc-unresumable",
                                name="ask_hermes",
                                args={"task": "book a table"},
                            )
                        ]
                    ),
                    tool_call_cancellation=None,
                    usage_metadata=None,
                    server_content=None,
                    go_away=None,
                )
                yield types.SimpleNamespace(
                    session_resumption_update=types.SimpleNamespace(
                        resumable=False,
                        new_handle=None,
                        last_consumed_client_message_index=9,
                    ),
                    tool_call=None,
                    tool_call_cancellation=None,
                    usage_metadata=None,
                    server_content=None,
                    go_away=types.SimpleNamespace(time_left="1s"),
                )
            else:
                manager._stop_event.set()
                if False:
                    yield None

    class ConnectContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return None

    class FakeLive:
        def connect(self, *, model, config):
            configs.append((model, config))
            scope_at_connect.append(
                {
                    "queued": manager._requests.qsize(),
                    "stale": len(manager._stale),
                    "timeouts": len(manager._tool_timeout_tasks),
                }
            )
            return ConnectContext(FakeSession(len(configs)))

    client = types.SimpleNamespace(aio=types.SimpleNamespace(live=FakeLive()))

    async def blocked_sender(_session):
        await manager._stop_event.wait()

    manager._gemini_input_sender = blocked_sender
    await manager._session_supervisor(client, "context")

    assert len(configs) == 2
    assert configs[0][1]["session_resumption"] == {}
    assert configs[1][1]["session_resumption"] == {}
    assert scope_at_connect[1] == {"queued": 0, "stale": 1, "timeouts": 0}
    assert manager._resumption_handle is None
    assert manager.status()["session_resumable"] is False
    assert manager.status()["session_resumable_now"] is False
    assert manager.status()["last_consumed_client_message_index"] == 9
    stale = manager.stale_requests()["requests"]
    assert stale[0]["request_id"] == "fc-unresumable"
    assert stale[0]["stale_reason"] == "gemini_cold_reconnect"
    assert manager._function_call_ids == {}
    assert manager._tool_response_outbox == {}


async def test_gemini_supervisor_reaps_provider_children_before_context_close():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    children = []
    all_started = asyncio.Event()
    blocker = asyncio.Event()
    loop_errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def sender(_session):
        children.append(asyncio.current_task())
        if len(children) == 2:
            all_started.set()
        await blocker.wait()

    async def receiver(_session):
        children.append(asyncio.current_task())
        if len(children) == 2:
            all_started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError as exc:
            # google-genai 2.16 converts even a clean provider close into an
            # APIError. The supervisor must still retrieve this exception.
            raise RuntimeError("APIError: 1000 None") from exc

    class ConnectContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class FakeLive:
        def connect(self, **_kwargs):
            return ConnectContext()

    manager._gemini_input_sender = sender
    manager._gemini_receiver = receiver
    client = types.SimpleNamespace(aio=types.SimpleNamespace(live=FakeLive()))
    supervisor = asyncio.create_task(manager._session_supervisor(client, ""))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert all(task is not None and task.done() for task in children)
    assert loop_errors == []


async def test_gemini_media_lease_is_released_exactly_once():
    released = []

    async def ok(*_args):
        return {"ok": True}

    async def release(stream_id):
        released.append(stream_id)
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
        release_stream=release,
    )
    manager._stream_info = {"stream_id": "gemini-stream-1"}
    manager._stream_released = False

    await manager._close_provider_session()
    await manager._close_provider_session()

    assert released == ["gemini-stream-1"]


async def test_gemini_releases_acquired_lease_when_stream_url_is_missing():
    released = []

    async def acquire(_session_id):
        return {"ok": True, "result": {"stream_id": "bad-stream"}}

    async def ok(*_args):
        return {"ok": True}

    async def release(stream_id):
        released.append(stream_id)
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=acquire,
        clear_playback=ok,
        hangup=ok,
        release_stream=release,
    )
    manager._availability = lambda: {"available": True, "reason": "ok"}

    result = await manager.start("call-1")

    assert result["error"] == "audio_stream_url_missing"
    assert released == ["bad-stream"]


def test_gemini_full_transcript_is_opt_in_but_summary_remains_available():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._last_session_id = "call-1"
    manager.add_transcript("input", "private caller text")

    transcript = manager.get_call_transcript("call-1")
    summary = manager.get_last_call_summary("call-1")

    assert transcript["error"] == "full_transcripts_disabled"
    assert transcript["events"] == []
    assert summary["event_count"] == 1
    assert summary["redacted"] is True
    assert summary["last_input_transcript"] is None
    assert manager.status()["last_input_transcript"] is None
    assert manager._transcript_events("call-1")[0]["text"] == "[redacted]"


def test_installed_sdk_accepts_full_gemini_31_live_config():
    google_types = pytest.importorskip("google.genai.types")
    config = gemini_live._live_config(
        "current call context",
        "resume-handle",
        set(gemini_live.HERMES_TOOL_NAMES),
    )

    validated = google_types.LiveConnectConfig(**config)

    assert validated.response_modalities[0].value == "AUDIO"
    assert validated.thinking_config.thinking_level.value == "MINIMAL"
    assert validated.context_window_compression.sliding_window.target_tokens == 8000
    for declaration in validated.tools[0].function_declarations:
        assert declaration.behavior is None


def test_gemini_live_config_exposes_voice_and_transcription_controls(monkeypatch):
    monkeypatch.setenv("HFP_GEMINI_LIVE_VOICE", "Autonoe")
    monkeypatch.setenv("HFP_GEMINI_INPUT_TRANSCRIPTION", "false")
    monkeypatch.setenv("HFP_GEMINI_OUTPUT_TRANSCRIPTION", "true")
    monkeypatch.setenv("HFP_GEMINI_THINKING_LEVEL", "low")

    config = gemini_live._live_config("", None, set())

    assert "input_audio_transcription" not in config
    assert config["output_audio_transcription"] == {}
    assert config["thinking_config"] == {
        "thinking_level": "low",
        "include_thoughts": False,
    }
    assert (
        config["speech_config"]["voice_config"]["prebuilt_voice_config"][
            "voice_name"
        ]
        == "Autonoe"
    )


def test_gemini_live_config_omits_speech_config_when_voice_is_unset(monkeypatch):
    monkeypatch.delenv("HFP_GEMINI_LIVE_VOICE", raising=False)

    config = gemini_live._live_config("", None, set())

    assert gemini_live.gemini_live_voice() is None
    assert "speech_config" not in config


async def test_audio_sender_marks_a_stream_pause_and_serializes_each_input(monkeypatch):
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    monkeypatch.setattr(gemini_live, "AUDIO_STREAM_IDLE_SECONDS", 0.01)
    calls = []

    class FakeSession:
        async def send_realtime_input(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("audio_stream_end"):
                manager._stop_event.set()

    manager._input_queue.put_nowait(b"\x00\x00" * 160)
    # The first lazy numpy/soxr import can take several seconds on a small Pi.
    await asyncio.wait_for(manager._gemini_input_sender(FakeSession()), timeout=8.0)

    assert calls[-1] == {"audio_stream_end": True}
    assert all(len(call) == 1 for call in calls)
    audio_calls = [call for call in calls if "audio" in call]
    assert audio_calls
    assert all(call["audio"].mime_type == "audio/pcm;rate=16000" for call in audio_calls)


async def test_audio_sender_accounts_for_dequeued_frames_when_provider_send_fails(
    monkeypatch,
):
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )

    class IdentityResampler:
        def __init__(self, *_args, **_kwargs):
            pass

        def convert(self, pcm, *, final=False):
            return pcm

    class FailedSession:
        async def send_realtime_input(self, **_kwargs):
            raise RuntimeError("provider disconnected")

    monkeypatch.setattr(gemini_live, "PcmResampler", IdentityResampler)
    manager._queue_input_frame(b"\x00" * gemini_live.HFP_FRAME_BYTES)

    with pytest.raises(RuntimeError, match="provider disconnected"):
        await manager._gemini_input_sender(FailedSession())

    status = manager.status()
    assert status["input_frames_received"] == 1
    assert status["input_frames_submitted"] == 0
    assert status["input_send_failure_frames"] == 1
    assert status["dropped_input_frames"] == 1


async def test_hfp_reader_splits_websocket_payload_into_20ms_frames():
    aiohttp = pytest.importorskip("aiohttp")

    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )

    class FakeWebSocket:
        def __aiter__(self):
            async def messages():
                yield types.SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY,
                    data=b"\x01\x00" * 320,
                )
                yield types.SimpleNamespace(type=aiohttp.WSMsgType.CLOSED)

            return messages()

    await manager._hfp_reader(FakeWebSocket())

    assert manager._input_queue.qsize() == 2
    assert len(manager._input_queue.get_nowait()) == gemini_live.HFP_FRAME_BYTES
    assert len(manager._input_queue.get_nowait()) == gemini_live.HFP_FRAME_BYTES


async def test_receiver_processes_content_before_goaway_and_retires_unsafe_handle():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
        full_transcripts_enabled=True,
    )
    manager._session_id = "call-1"
    manager._resumption_handle = "last-good"
    manager._resumable_now = True
    usage = types.SimpleNamespace(
        prompt_token_count=10,
        cached_content_token_count=2,
        response_token_count=3,
        tool_use_prompt_token_count=1,
        thoughts_token_count=0,
        total_token_count=16,
        traffic_type=None,
        service_tier=None,
    )
    parts = [
        types.SimpleNamespace(
            inline_data=types.SimpleNamespace(
                data=b"\x00\x00" * 480,
                mime_type="audio/pcm;rate=24000",
            )
        ),
        types.SimpleNamespace(
            inline_data=types.SimpleNamespace(
                data=b"\x00\x00" * 480,
                mime_type="audio/pcm;rate=24000",
            )
        ),
    ]
    response = types.SimpleNamespace(
        session_resumption_update=types.SimpleNamespace(
            resumable=False, new_handle=None
        ),
        tool_call=None,
        tool_call_cancellation=None,
        usage_metadata=usage,
        server_content=types.SimpleNamespace(
            interim_input_transcription=types.SimpleNamespace(
                text="hel", finished=False
            ),
            input_transcription=types.SimpleNamespace(
                text="hello", finished=True
            ),
            output_transcription=types.SimpleNamespace(
                text="hi", finished=True
            ),
            interrupted=False,
            model_turn=types.SimpleNamespace(parts=parts),
            generation_complete=True,
            turn_complete=True,
        ),
        go_away=types.SimpleNamespace(time_left="2s"),
    )

    class FakeSession:
        async def receive(self):
            yield response

    with pytest.raises(gemini_live._ReconnectRequested, match="gemini_go_away"):
        await manager._gemini_receiver(FakeSession())

    assert manager._resumption_handle is None
    assert manager.status()["session_resumable"] is False
    assert manager.status()["session_resumable_now"] is False
    assert manager.status()["generation_count"] == 1
    assert manager.status()["usage_metadata"]["total_token_count"] == 16
    assert manager.status()["last_go_away_time_left"] == "2s"
    assert [
        item["text"] for item in manager.get_call_transcript("call-1")["events"]
    ] == ["hello", "hi"]
    assert manager._playback_queue.qsize() >= 2


async def test_playback_epoch_drops_a_frame_dequeued_before_interruption():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    sent = []

    class FakeWebSocket:
        async def send_bytes(self, frame):
            sent.append(frame)

    manager._enqueue_playback_pcm(b"a" * gemini_live.HFP_FRAME_BYTES)
    manager._enqueue_playback_pcm(b"b" * gemini_live.HFP_FRAME_BYTES)
    writer = asyncio.create_task(manager._playback_writer(FakeWebSocket()))
    while len(sent) < 1 or not manager._playback_queue.empty():
        await asyncio.sleep(0)
    await manager._handle_interruption()
    await asyncio.sleep(0.03)
    manager._stop_event.set()
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    assert sent == [b"a" * gemini_live.HFP_FRAME_BYTES]


async def test_gemini_writer_primes_each_utterance_before_paced_playout():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    events = []

    class FakeWebSocket:
        async def send_bytes(self, frame):
            events.append(("audio", frame))

        async def send_str(self, value):
            events.append(("control", json.loads(value)))

    window_id = manager._begin_playback_window()
    frames = [
        bytes([index]) * gemini_live.HFP_FRAME_BYTES
        for index in range(gemini_live.PLAYBACK_PREBUFFER_FRAMES)
    ]
    manager._enqueue_playback_pcm(b"".join(frames), window_id=window_id)
    manager._end_playback_window(window_id)

    writer = asyncio.create_task(manager._playback_writer(FakeWebSocket()))
    while len(events) < 3:
        await asyncio.sleep(0)
    manager._stop_event.set()
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    assert events == [
        (
            "control",
            {"type": "playback_start", "utterance_id": window_id},
        ),
        ("audio", b"".join(frames)),
        (
            "control",
            {"type": "playback_end", "utterance_id": window_id},
        ),
    ]
    status = manager.status()
    assert status["playback_prebuffer_events"] == 1
    assert status["playback_prebuffer_frames"] == 8
    assert status["playback_frames_sent"] == 8


async def test_gemini_interruption_cancels_frames_held_during_prebuffer():
    clears = []

    async def ok(*args):
        clears.append(args)
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    manager._stream_info = {"stream_id": "gemini-stream-1"}
    events = []

    class FakeWebSocket:
        async def send_bytes(self, frame):
            events.append(("audio", frame))

        async def send_str(self, value):
            events.append(("control", json.loads(value)))

    window_id = manager._begin_playback_window()
    manager._enqueue_playback_pcm(
        b"a" * gemini_live.HFP_FRAME_BYTES, window_id=window_id
    )
    writer = asyncio.create_task(manager._playback_writer(FakeWebSocket()))
    while not events:
        await asyncio.sleep(0)

    await manager._handle_interruption()
    await asyncio.sleep(0.03)
    manager._stop_event.set()
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    assert events == [
        (
            "control",
            {"type": "playback_start", "utterance_id": window_id},
        )
    ]
    assert clears == [("gemini-stream-1",)]


async def test_gemini_interruption_clear_is_ordered_after_an_inflight_send():
    events = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def ensure(*_args):
        return {"ok": True}

    async def clear(*_args):
        events.append("clear")
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ensure, clear_playback=clear, hangup=ensure
    )
    manager._session_id = "call-1"
    manager._stream_info = {"stream_id": "gemini-stream-1"}

    class FakeWebSocket:
        async def send_str(self, _value):
            events.append("start")

        async def send_bytes(self, _frame):
            send_started.set()
            await release_send.wait()
            events.append("audio")

    window_id = manager._begin_playback_window()
    manager._enqueue_playback_pcm(
        b"a" * gemini_live.HFP_FRAME_BYTES * 8,
        window_id=window_id,
    )
    writer = asyncio.create_task(manager._playback_writer(FakeWebSocket()))
    await send_started.wait()
    interruption = asyncio.create_task(manager._handle_interruption())
    await asyncio.sleep(0)
    assert "clear" not in events

    release_send.set()
    await interruption
    manager._stop_event.set()
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    assert events == ["start", "audio", "clear"]


def test_gemini_input_queue_is_fresh_and_reports_overflow_separately():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    frames = [bytes([index]) * gemini_live.HFP_FRAME_BYTES for index in range(15)]
    for frame in frames:
        manager._queue_input_frame(frame)

    retained = [manager._input_queue.get_nowait() for _ in range(10)]
    status = manager.status()
    assert retained == frames[-10:]
    assert status["input_frames_received"] == 15
    assert status["input_queue_overflow_frames"] == 5
    assert status["input_startup_trimmed_frames"] == 0
    assert status["input_queue_peak_ms"] == 200
    assert status["dropped_input_frames"] == 5


def test_gemini_input_startup_and_reconnect_trims_are_distinct():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._input_queue = asyncio.Queue(maxsize=50)
    for index in range(35):
        manager._queue_input_frame(
            bytes([index]) * gemini_live.HFP_FRAME_BYTES
        )
    manager._trim_input_queue(5, reason="startup")
    for index in range(10):
        manager._queue_input_frame(
            bytes([index]) * gemini_live.HFP_FRAME_BYTES
        )
    manager._trim_input_queue(5, reason="reconnect")

    status = manager.status()
    assert status["input_startup_trimmed_frames"] == 30
    assert status["input_reconnect_trimmed_frames"] == 10
    assert status["input_queue_overflow_frames"] == 0
    assert status["dropped_input_frames"] == 40


async def test_tool_deadline_sends_exact_timeout_response_and_expires_request(
    monkeypatch,
):
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    monkeypatch.setattr(gemini_live, "gemini_tool_deadline_seconds", lambda: 0.01)
    sent = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    manager._live_session = FakeSession()
    await manager._handle_tool_calls(
        types.SimpleNamespace(
            tool_call=types.SimpleNamespace(
                function_calls=[
                    types.SimpleNamespace(
                        id="fc-timeout", name="ask_hermes", args={"task": "slow"}
                    )
                ]
            )
        )
    )
    await asyncio.sleep(0.03)

    assert sent[0].id == "fc-timeout"
    assert sent[0].name == "ask_hermes"
    assert sent[0].response["status"] == "error"
    stale = manager.stale_requests()["requests"]
    assert stale[0]["state"] == "expired"
    assert stale[0]["stale_reason"] == "tool_deadline_expired"


async def test_completed_tool_redelivery_replays_cached_result_without_requeue():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    sent = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    manager._live_session = FakeSession()
    blocker = asyncio.Event()
    manager._task = asyncio.create_task(blocker.wait())
    manager._state = gemini_live.LiveAIState.RUNNING
    response = types.SimpleNamespace(
        tool_call=types.SimpleNamespace(
            function_calls=[
                types.SimpleNamespace(
                    id="fc-replay",
                    name="get_hermes_context",
                    args={"topic": "weather"},
                )
            ]
        )
    )
    await manager._handle_tool_calls(response)
    await manager.poll_requests(0.01)
    submitted = await manager.submit_result(
        "fc-replay", {"status": "ok", "message": "cached"}
    )
    await manager._handle_tool_calls(response)
    duplicate_poll = await manager.poll_requests(0.01)
    manager._cancel_all_tool_tasks()
    manager._task.cancel()
    await asyncio.gather(manager._task, return_exceptions=True)

    assert submitted["ok"] is True
    assert duplicate_poll["requests"] == []
    assert len(sent) == 2
    assert sent[0].response == sent[1].response
    bridge = manager.status()["tool_bridge"]
    assert bridge["calls_received"] == 2
    assert bridge["calls_accepted"] == 1
    assert bridge["results_submitted"] == 1
    assert bridge["responses_sent"] == 2


async def test_tool_call_without_provider_id_is_protocol_error():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    response = types.SimpleNamespace(
        tool_call=types.SimpleNamespace(
            function_calls=[types.SimpleNamespace(id=None, name="ask_hermes", args={})]
        )
    )

    with pytest.raises(
        gemini_live._ReconnectRequested, match="gemini_function_call_missing_id"
    ):
        await manager._handle_tool_calls(response)
    assert manager.status()["function_protocol_errors"] == 1
    assert manager._requests.empty()


async def test_client_cancellation_unblocks_synchronous_function_call():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    sent = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    manager._live_session = FakeSession()
    await manager._handle_tool_calls(
        types.SimpleNamespace(
            tool_call=types.SimpleNamespace(
                function_calls=[
                    types.SimpleNamespace(
                        id="fc-cancel", name="ask_hermes", args={"task": "stop"}
                    )
                ]
            )
        )
    )

    result = manager.cancel_request("fc-cancel", "operator_cancelled")
    await asyncio.gather(*list(manager._tool_resolution_tasks))

    assert result["ok"] is True
    assert sent[0].id == "fc-cancel"
    assert sent[0].response["status"] == "cancelled"
    assert sent[0].response["speak_to_caller"] is False


async def test_undeclared_tool_gets_exact_error_without_broker_execution():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
        allowed_tools=set(),
    )
    sent = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    manager._live_session = FakeSession()
    await manager._handle_tool_calls(
        types.SimpleNamespace(
            tool_call=types.SimpleNamespace(
                function_calls=[
                    types.SimpleNamespace(
                        id="fc-blocked", name="ask_hermes", args={"task": "unsafe"}
                    )
                ]
            )
        )
    )

    assert manager._requests.empty()
    assert sent[0].id == "fc-blocked"
    assert sent[0].response["status"] == "error"
    assert sent[0].response["speak_to_caller"] is False
    bridge = manager.status()["tool_bridge"]
    assert bridge["responses_sent"] == 1
    assert bridge["last_state"] == "rejected"
    assert "not declared" in bridge["last_error"]


async def test_pending_overflow_returns_error_instead_of_blocking_provider():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    manager._pending_request_limit = 1
    sent = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    manager._live_session = FakeSession()

    async def add(function_call_id):
        await manager._handle_tool_calls(
            types.SimpleNamespace(
                tool_call=types.SimpleNamespace(
                    function_calls=[
                        types.SimpleNamespace(
                            id=function_call_id,
                            name="ask_hermes",
                            args={"task": function_call_id},
                        )
                    ]
                )
            )
        )

    await add("fc-first")
    await manager.poll_requests(0.01)
    await add("fc-second")
    await manager.poll_requests(0.01)

    assert sent[0].id == "fc-first"
    assert sent[0].response["status"] == "error"
    assert manager.pending_requests()["requests"][0]["request_id"] == "fc-second"
    manager._cancel_all_tool_tasks()


async def test_cold_reconnect_stales_requests_from_old_provider_session():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok, clear_playback=ok, hangup=ok
    )
    manager._session_id = "call-1"
    configs = []

    class FakeSession:
        def __init__(self, generation):
            self.generation = generation
            self.receive_calls = 0

        async def receive(self):
            if self.generation == 1:
                self.receive_calls += 1
                if self.receive_calls > 1:
                    raise RuntimeError("first websocket closed")
                yield types.SimpleNamespace(
                    session_resumption_update=None,
                    tool_call=types.SimpleNamespace(
                        function_calls=[
                            types.SimpleNamespace(
                                id="fc-old",
                                name="ask_hermes",
                                args={"task": "old"},
                            )
                        ]
                    ),
                    tool_call_cancellation=None,
                    usage_metadata=None,
                    server_content=None,
                    go_away=None,
                )
            else:
                manager._stop_event.set()
                if False:
                    yield None

    class ConnectContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return None

    class FakeLive:
        def connect(self, *, model, config):
            configs.append((model, config))
            return ConnectContext(FakeSession(len(configs)))

    client = types.SimpleNamespace(aio=types.SimpleNamespace(live=FakeLive()))

    async def blocked_sender(_session):
        await manager._stop_event.wait()

    manager._gemini_input_sender = blocked_sender
    await manager._session_supervisor(client, "")

    assert len(configs) == 2
    stale = manager.stale_requests()["requests"]
    assert stale[0]["request_id"] == "fc-old"
    assert stale[0]["stale_reason"] == "gemini_cold_reconnect"


def test_structured_tool_outcome_enforces_total_encoded_size():
    outcome = gemini_live._structured_tool_outcome(
        {
            "status": "ok",
            "message": "done",
            "unexpected_huge_key": "x" * (gemini_live.MAX_TOOL_RESPONSE_BYTES * 2),
        },
        speak_to_caller=True,
    )

    assert outcome["truncated"] is True
    assert "unexpected_huge_key" not in outcome
    assert len(json.dumps(outcome).encode("utf-8")) <= gemini_live.MAX_TOOL_RESPONSE_BYTES


async def test_conversation_refresh_replaces_provider_context_without_hanging_up():
    phone_actions, ready, configs = [], [], []
    async def phone_action(*args):
        phone_actions.append(args)
        return {"ok": True}
    contexts = ["old phone dialogue", "fresh phone dialogue"]
    async def context():
        return contexts[min(len(configs), 1)]
    async def clear(*_):
        return {"ok": True}
    manager = gemini_live.GeminiLiveManager(
        ensure_stream=phone_action, clear_playback=clear, hangup=phone_action,
        context_provider=context, context_ready=ready.append,
        allowed_tools={"ask_hermes", "phone_session", "phone_recall", "hermes_task"},
    )
    manager._session_id = "physical-call"
    manager.add_transcript("input", "old private dialogue")
    class Session:
        async def receive(self):
            if len(configs) == 1:
                manager._resumption_handle = "must-not-resume-old-chat"
                manager._resumable_now = True
                await manager.reset_conversation("fresh phone dialogue", 1)
                await asyncio.Future()
            else:
                manager._stop_event.set()
            if False:
                yield None
    class Connection:
        async def __aenter__(self): return Session()
        async def __aexit__(self, *_): pass
    class Live:
        def connect(self, *, model, config):
            configs.append(config)
            return Connection()
    async def sender(_):
        await asyncio.Future()
    manager._gemini_input_sender = sender
    await asyncio.wait_for(manager._session_supervisor(types.SimpleNamespace(
        aio=types.SimpleNamespace(live=Live())), "initial"), 2)
    assert len(configs) == 2
    assert "fresh phone dialogue" in configs[1]["system_instruction"]
    assert "old phone dialogue" not in configs[1]["system_instruction"]
    assert not configs[1]["session_resumption"].get("handle")
    assert ready == [0, 1]
    assert manager._session_id == "physical-call"
    assert not manager._transcripts
    assert manager.consume_summary_candidate() is None
    assert not phone_actions
