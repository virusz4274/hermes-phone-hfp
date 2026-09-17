import asyncio

from hfp_mcp.live_ai import LiveAIManager, LiveAIRequest, LiveAIState


class FakeLiveAIManager(LiveAIManager):
    def __init__(self) -> None:
        super().__init__(
            provider="fake",
            model=lambda: "fake-live-model",
            availability=lambda: {"available": True, "reason": "ok"},
            ensure_stream=self._ensure_stream,
            clear_playback=self._clear_playback,
            hangup=self._hangup,
        )
        self.submitted = []
        self.sent_text = []
        self.cleared = []
        self.hung_up = False

    async def _ensure_stream(self, session_id: str) -> dict:
        return {"ok": True, "stream_url": f"ws://example.test/{session_id}"}

    async def _clear_playback(self, session_id: str) -> dict:
        self.cleared.append(session_id)
        return {"ok": True}

    async def _hangup(self) -> dict:
        self.hung_up = True
        return {"ok": True}

    async def _run_provider(self, stream_url: str, initial_context: str) -> None:
        self._mark_ready()
        await self._stop_event.wait()

    async def _send_provider_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def _submit_provider_result(
        self,
        request: LiveAIRequest,
        result: str,
        *,
        speak_to_caller: bool,
    ) -> None:
        self.submitted.append((request.request_id, result, speak_to_caller))


def _request(request_id: str = "req-1") -> LiveAIRequest:
    return LiveAIRequest(
        request_id=request_id,
        function_call_id=f"fc-{request_id}",
        name="ask_mcp_client",
        arguments={"task": "check status"},
        created_at=123.0,
        session_id="call-1",
        provider="fake",
    )


async def test_live_ai_request_counts_move_from_queued_to_pending_to_resolved():
    manager = FakeLiveAIManager()
    await manager.start("call-1")
    manager.add_request(_request())

    before_poll = manager.status({"call_state": "active", "call_active": True, "audio_active": True})
    assert before_poll["queued_requests"] == 1
    assert before_poll["pending_requests"] == 0
    assert before_poll["total_unresolved_requests"] == 1

    polled = await manager.poll_requests(timeout_seconds=0.01)
    after_poll = manager.status({"call_state": "active", "call_active": True, "audio_active": True})
    assert [item["request_id"] for item in polled["requests"]] == ["req-1"]
    assert after_poll["queued_requests"] == 0
    assert after_poll["pending_requests"] == 1

    submitted = await manager.submit_result("req-1", "done", speak_to_caller=False)
    after_submit = manager.status({"call_state": "active", "call_active": True, "audio_active": True})
    assert submitted["ok"] is True
    assert manager.submitted == [("req-1", "done", False)]
    assert after_submit["total_unresolved_requests"] == 0

    await manager.stop("test complete")


async def test_live_ai_cancel_marks_request_stale():
    manager = FakeLiveAIManager()
    manager.add_request(_request())

    result = manager.cancel_request("req-1", "operator_cancelled")

    status = manager.status()
    stale = manager.stale_requests()["requests"]
    assert result["ok"] is True
    assert status["queued_requests"] == 0
    assert status["stale_requests"] == 1
    assert stale[0]["stale"] is True
    assert stale[0]["stale_reason"] == "operator_cancelled"


async def test_live_ai_stop_marks_pending_requests_stale():
    manager = FakeLiveAIManager()
    await manager.start("call-1")
    manager.add_request(_request())
    await manager.poll_requests(timeout_seconds=0.01)

    result = await manager.stop("call_ended")

    status = manager.status({"call_state": "idle", "call_active": False, "audio_active": False})
    stale = manager.stale_requests()["requests"]
    assert result["ok"] is True
    assert status["pending_requests"] == 0
    assert status["stale_requests"] == 1
    assert status["session_stale"] is True
    assert stale[0]["session_active"] is False
    assert stale[0]["stale_reason"] == "call_ended"


def test_live_ai_transcript_and_summary_are_deterministic():
    manager = FakeLiveAIManager()
    manager._last_session_id = "call-1"

    manager.add_transcript("input", "hello")
    manager.add_transcript("output", "hi there")

    transcript = manager.get_call_transcript("call-1")
    summary = manager.get_last_call_summary("call-1")
    assert [event["text"] for event in transcript["events"]] == ["hello", "hi there"]
    assert summary["event_count"] == 2
    assert summary["input_event_count"] == 1
    assert summary["output_event_count"] == 1
    assert summary["last_input_transcript"] == "hello"
    assert summary["last_output_transcript"] == "hi there"


async def test_live_ai_instruction_defaults_to_internal_context():
    manager = FakeLiveAIManager()
    await manager.start("call-1")

    result = await manager.send_text("do not say this")

    assert result["ok"] is True
    assert manager.sent_text == [
        "Internal context update for the assistant. Do not speak this verbatim: do not say this"
    ]

    await manager.stop("done")


async def test_repeated_start_delivers_late_context_to_running_session():
    manager = FakeLiveAIManager()
    started = await manager.start("call-1", "generic setup context")

    result = await manager.start(
        "call-1",
        "Immediately ask whether the computer is functioning normally.",
    )

    assert started["initial_context_applied"] is True
    assert started["initial_context_delivery"] == "setup_system_instruction"
    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["initial_context_applied"] is True
    assert result["initial_context_delivery"] == "realtime_text"
    assert manager.sent_text == [
        "Internal context update for the assistant. Do not speak this verbatim: "
        "Immediately ask whether the computer is functioning normally."
    ]

    await manager.stop("done")


async def test_repeated_start_reports_late_context_delivery_failure():
    manager = FakeLiveAIManager()
    await manager.start("call-1")

    async def reject_context(_text: str) -> None:
        raise RuntimeError("provider text channel closed")

    manager._send_provider_text = reject_context
    result = await manager.start("call-1", "Ask the caller a question.")

    assert result["ok"] is False
    assert result["already_running"] is True
    assert result["initial_context_applied"] is False
    assert result["initial_context_delivery"] == "failed"
    assert result["error"] == "live_ai_context_update_failed"
    assert result["context_error"] == "provider text channel closed"
    assert manager.running is True

    await manager.stop("done")


async def test_live_ai_start_waits_until_provider_is_ready():
    manager = FakeLiveAIManager()
    original = manager._run_provider

    async def delayed_provider(stream_url: str, initial_context: str) -> None:
        await asyncio.sleep(0.02)
        await original(stream_url, initial_context)

    manager._run_provider = delayed_provider
    start = asyncio.create_task(manager.start("call-ready"))
    await asyncio.sleep(0)

    assert not start.done()
    assert manager.lifecycle_state is LiveAIState.STARTING

    result = await start
    assert result["ok"] is True
    assert result["ready"] is True
    assert manager.status()["state"] == "running"
    await manager.stop("done")


async def test_live_ai_prepares_provider_before_acquiring_audio_stream():
    manager = FakeLiveAIManager()
    events = []
    original_ensure_stream = manager._ensure_stream

    async def prepare_provider() -> None:
        events.append("prepare")

    async def ensure_stream(session_id: str) -> dict:
        events.append("stream")
        return await original_ensure_stream(session_id)

    manager._prepare_provider = prepare_provider
    manager._ensure_stream = ensure_stream

    result = await manager.start("call-warm")

    assert result["ok"] is True
    assert events == ["prepare", "stream"]
    await manager.stop("done")


async def test_live_ai_provider_prepare_failure_does_not_start_audio():
    manager = FakeLiveAIManager()
    stream_started = False

    async def prepare_provider() -> None:
        raise RuntimeError("resampler warmup failed")

    async def ensure_stream(_session_id: str) -> dict:
        nonlocal stream_started
        stream_started = True
        return {"ok": True}

    manager._prepare_provider = prepare_provider
    manager._ensure_stream = ensure_stream

    result = await manager.start("call-cold")

    assert result == {
        "ok": False,
        "error": "provider_prepare_failed: resampler warmup failed",
    }
    assert stream_started is False


async def test_live_ai_stop_during_provider_prepare_never_acquires_audio():
    manager = FakeLiveAIManager()
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    stream_started = False

    async def prepare_provider() -> None:
        prepare_started.set()
        await release_prepare.wait()

    async def ensure_stream(_session_id: str) -> dict:
        nonlocal stream_started
        stream_started = True
        return {"ok": True}

    manager._prepare_provider = prepare_provider
    manager._ensure_stream = ensure_stream
    starting = asyncio.create_task(manager.start("call-cancel-warmup"))
    await prepare_started.wait()
    stopping = asyncio.create_task(manager.stop("operator_cancelled"))
    await asyncio.sleep(0)
    release_prepare.set()

    assert await starting == {
        "ok": False,
        "error": "live_ai_start_cancelled",
    }
    assert (await stopping)["ok"] is True
    assert stream_started is False


async def test_live_ai_provider_start_failure_is_reported_not_false_success():
    manager = FakeLiveAIManager()

    async def failed_provider(_stream_url: str, _initial_context: str) -> None:
        raise RuntimeError("invalid credential")

    manager._run_provider = failed_provider
    result = await manager.start("call-failed")

    assert result == {"ok": False, "error": "invalid credential"}
    assert manager.status()["state"] == "failed"


async def test_live_ai_stop_cancels_a_concurrent_start_without_timeout():
    manager = FakeLiveAIManager()

    async def never_ready(_stream_url: str, _initial_context: str) -> None:
        await asyncio.Event().wait()

    manager._run_provider = never_ready
    start_task = asyncio.create_task(manager.start("call-cancel"))
    while manager.lifecycle_state is not LiveAIState.STARTING:
        await asyncio.sleep(0)

    stop_result = await asyncio.wait_for(manager.stop("operator_cancelled"), timeout=0.5)
    start_result = await asyncio.wait_for(start_task, timeout=0.5)

    assert stop_result["ok"] is True
    assert start_result == {"ok": False, "error": "live_ai_start_cancelled"}
    assert manager.lifecycle_state is LiveAIState.STOPPED


async def test_live_ai_signal_stop_does_not_misclassify_clean_provider_exit():
    manager = FakeLiveAIManager()
    await manager.start("call-ended")

    manager.signal_stop("call_ended")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    status = manager.status()
    assert status["running"] is False
    assert status["state"] != LiveAIState.FAILED.value
    assert status["last_error"] is None

    stopped = await manager.stop("call_ended")
    assert stopped["ok"] is True
    assert manager.lifecycle_state is LiveAIState.STOPPED


def test_live_ai_transcripts_and_request_queue_are_bounded():
    manager = FakeLiveAIManager()
    manager._requests = asyncio.Queue(maxsize=2)
    manager._transcripts = manager._transcripts.__class__(maxlen=2)
    manager._last_session_id = "call-1"

    assert manager.add_request(_request("req-1")) is True
    assert manager.add_request(_request("req-2")) is True
    assert manager.add_request(_request("req-3")) is False
    manager.add_transcript("input", "one")
    manager.add_transcript("input", "two")
    manager.add_transcript("input", "three")

    assert manager.status()["queued_requests"] == 2
    assert manager.status()["dropped_requests"] == 1
    assert manager.stale_requests()["requests"][0]["stale_reason"] == "queue_overflow"
    assert [item["text"] for item in manager.get_call_transcript()["events"]] == [
        "two",
        "three",
    ]


def test_live_ai_transcription_fragments_are_assembled_once_per_turn():
    manager = FakeLiveAIManager()
    manager._last_session_id = "call-1"

    manager.append_transcript_fragment("input", "hel")
    manager.append_transcript_fragment("input", "lo", final=True)

    assert manager.get_call_transcript()["events"][0]["text"] == "hello"


def test_private_summary_candidate_never_leaks_through_transcript_apis():
    manager = FakeLiveAIManager()
    manager._full_transcripts_enabled = False
    manager._last_session_id = "call-private"

    manager.append_transcript_fragment("input", "Please call me at ")
    manager.append_transcript_fragment("input", "+1 415 555 0100", final=True)

    transcript = manager.get_call_transcript("call-private")
    summary = manager.get_last_call_summary("call-private")
    assert transcript["ok"] is False
    assert transcript["events"] == []
    assert summary["last_input_transcript"] is None
    assert manager._transcripts[0].text == "[redacted]"
    assert manager.consume_summary_candidate("another-call") is None
    assert manager.consume_summary_candidate("call-private") == (
        "Please call me at +1 415 555 0100"
    )
    assert manager.consume_summary_candidate("call-private") is None
