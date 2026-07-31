from hfp_mcp.live_ai import LiveAIManager, LiveAIRequest


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
