"""Provider-neutral live AI session state for HFP calls."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


EnsureStream = Callable[[str], Awaitable[dict]]
ClearPlayback = Callable[[str], Awaitable[dict]]
HangupCall = Callable[[], Awaitable[dict]]
Availability = Callable[[], dict]
ModelName = Callable[[], str]


@dataclass
class LiveAIRequest:
    request_id: str
    function_call_id: str
    name: str
    arguments: dict
    created_at: float
    session_id: str = ""
    provider: str = "live_ai"
    state: str = "queued"
    session_active: bool = True
    stale: bool = False
    stale_reason: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "request_id": self.request_id,
            "name": self.name,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "session_id": self.session_id or None,
            "provider": self.provider,
            "state": self.state,
            "session_active": self.session_active,
            "stale": self.stale,
        }
        if self.stale_reason:
            payload["stale_reason"] = self.stale_reason
        return payload

    def mark_pending(self) -> None:
        self.state = "pending"

    def mark_stale(self, reason: str) -> None:
        self.state = "stale"
        self.session_active = False
        self.stale = True
        self.stale_reason = reason


@dataclass
class TranscriptEvent:
    timestamp: float
    session_id: str
    provider: str
    direction: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "provider": self.provider,
            "direction": self.direction,
            "text": self.text,
            "metadata": self.metadata,
        }


class LiveAIManager:
    """Owns one provider-backed live AI call session."""

    def __init__(
        self,
        *,
        provider: str,
        model: ModelName,
        availability: Availability,
        ensure_stream: EnsureStream,
        clear_playback: ClearPlayback,
        hangup: HangupCall,
    ) -> None:
        self.provider = provider
        self._model = model
        self._availability = availability
        self._ensure_stream = ensure_stream
        self._clear_playback = clear_playback
        self._hangup = hangup
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._ws = None
        self._session_id = ""
        self._last_session_id = ""
        self._started_at: float | None = None
        self._last_error = ""
        self._last_input_transcript = ""
        self._last_output_transcript = ""
        self._requests: asyncio.Queue[LiveAIRequest] = asyncio.Queue()
        self._pending: dict[str, LiveAIRequest] = {}
        self._stale: dict[str, LiveAIRequest] = {}
        self._transcripts: list[TranscriptEvent] = []

    def status(self, call_context: dict | None = None) -> dict:
        avail = self._availability()
        running = self.running
        queued = self._requests.qsize()
        pending = len(self._pending)
        stale = len(self._stale)
        call_context = call_context or {}
        call_state = call_context.get("call_state")
        call_active = bool(call_context.get("call_active"))
        audio_active = bool(call_context.get("audio_active"))
        return {
            "ok": True,
            "provider": self.provider,
            "available": bool(avail.get("available")),
            "availability_reason": avail.get("reason"),
            "model": self._model(),
            "running": running,
            "session_id": self._session_id or self._last_session_id or None,
            "uptime_seconds": (
                max(0.0, time.monotonic() - self._started_at)
                if self._started_at is not None and running
                else None
            ),
            "queued_requests": queued,
            "pending_requests": pending,
            "stale_requests": stale,
            "total_unresolved_requests": queued + pending + stale,
            "call_state": call_state,
            "call_active": call_active,
            "audio_active": audio_active,
            "session_stale": bool(stale) or (
                bool(self._last_session_id) and not running and not call_active
            ),
            "last_error": self._last_error or None,
            "last_input_transcript": self._last_input_transcript or None,
            "last_output_transcript": self._last_output_transcript or None,
        }

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def pending_requests(self) -> dict:
        return {
            "ok": True,
            "requests": [item.to_dict() for item in self._pending.values()],
        }

    def stale_requests(self) -> dict:
        return {
            "ok": True,
            "requests": [item.to_dict() for item in self._stale.values()],
        }

    async def start(
        self,
        session_id: str = "active-call",
        initial_context: str | None = None,
    ) -> dict:
        avail = self._availability()
        if not avail.get("available"):
            return {"ok": False, "error": avail.get("reason") or "live_ai_unavailable"}
        if self.running:
            return {
                "ok": True,
                "provider": self.provider,
                "session_id": self._session_id,
                "already_running": True,
            }

        stream = await self._ensure_stream(session_id)
        if not stream.get("ok"):
            return stream
        stream_url = stream.get("client_stream_url") or stream.get("stream_url")
        if not stream_url:
            return {"ok": False, "error": "audio_stream_url_missing"}

        self._stop_event = asyncio.Event()
        self._session_id = session_id
        self._last_session_id = session_id
        self._started_at = time.monotonic()
        self._last_error = ""
        self._task = asyncio.create_task(
            self._run_provider(stream_url, initial_context or ""),
            name=f"{self.provider}-live-{session_id}",
        )
        self._task.add_done_callback(self._task_done)
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": session_id,
            "model": self._model(),
        }

    async def stop(self, reason: str | None = None, hangup: bool = False) -> dict:
        stop_reason = reason or "session_stopped"
        self.mark_session_stale(stop_reason)
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self._close_provider_session()
        if self._session_id:
            await self._clear_playback(self._session_id)
        if hangup:
            await self._hangup()
        session_id, self._session_id = self._session_id, ""
        self._started_at = None
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": session_id or None,
            "reason": reason,
        }

    async def send_text(
        self,
        text: str,
        *,
        urgency: str = "normal",
        speak_to_caller: bool = False,
    ) -> dict:
        if not text.strip():
            return {"ok": False, "error": "empty_text"}
        if not self.running:
            return {"ok": False, "error": "live_ai_not_running"}
        if urgency == "urgent" and self._session_id:
            await self._clear_playback(self._session_id)
        prefix = ""
        if not speak_to_caller:
            prefix = "Internal context update for the assistant. Do not speak this verbatim: "
        try:
            await self._send_provider_text(f"{prefix}{text}")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "provider": self.provider}

    async def speak(self, text: str, *, urgency: str = "normal") -> dict:
        return await self.send_text(text, urgency=urgency, speak_to_caller=True)

    async def poll_requests(self, timeout_seconds: float = 5.0) -> dict:
        timeout = max(0.0, float(timeout_seconds))
        requests: list[dict] = []
        try:
            first = await asyncio.wait_for(self._requests.get(), timeout=timeout)
            first.mark_pending()
            self._pending[first.request_id] = first
            requests.append(first.to_dict())
        except asyncio.TimeoutError:
            return {"ok": True, "requests": []}

        while True:
            try:
                item = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            item.mark_pending()
            self._pending[item.request_id] = item
            requests.append(item.to_dict())
        return {"ok": True, "requests": requests}

    async def submit_result(
        self,
        request_id: str,
        result: str,
        *,
        speak_to_caller: bool = True,
    ) -> dict:
        request = self._pending.get(request_id)
        if request is None:
            if request_id in self._stale:
                return {"ok": False, "error": "request_stale"}
            return {"ok": False, "error": "request_not_found"}
        if not self.running:
            return {"ok": False, "error": "live_ai_not_running"}
        try:
            await self._submit_provider_result(
                request,
                result,
                speak_to_caller=speak_to_caller,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._pending.pop(request_id, None)
        return {"ok": True, "provider": self.provider}

    def cancel_request(self, request_id: str, reason: str = "cancelled") -> dict:
        request = self._pending.pop(request_id, None)
        if request is None:
            request = self._remove_queued_request(request_id)
        if request is None:
            if request_id in self._stale:
                return {"ok": True, "request_id": request_id, "already_stale": True}
            return {"ok": False, "error": "request_not_found"}
        request.mark_stale(reason or "cancelled")
        self._stale[request_id] = request
        return {"ok": True, "request_id": request_id, "reason": request.stale_reason}

    def clear_requests(
        self,
        session_id: str | None = None,
        *,
        only_stale: bool = True,
    ) -> dict:
        removed = 0
        for request_id, request in list(self._stale.items()):
            if session_id is None or request.session_id == session_id:
                self._stale.pop(request_id, None)
                removed += 1
        if not only_stale:
            for request_id, request in list(self._pending.items()):
                if session_id is None or request.session_id == session_id:
                    self._pending.pop(request_id, None)
                    removed += 1
            remaining: list[LiveAIRequest] = []
            while True:
                try:
                    request = self._requests.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if session_id is None or request.session_id == session_id:
                    removed += 1
                else:
                    remaining.append(request)
            for request in remaining:
                self._requests.put_nowait(request)
        return {"ok": True, "requests_cleared": removed}

    def mark_session_stale(self, reason: str = "session_stale") -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            request.mark_stale(reason)
            self._stale[request.request_id] = request
        for request_id, request in list(self._pending.items()):
            request.mark_stale(reason)
            self._stale[request_id] = request
            self._pending.pop(request_id, None)

    def add_request(self, request: LiveAIRequest) -> None:
        if not request.session_id:
            request.session_id = self._session_id or self._last_session_id
        request.provider = self.provider
        self._requests.put_nowait(request)

    def add_transcript(
        self,
        direction: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        session_id = self._session_id or self._last_session_id or "unknown"
        event = TranscriptEvent(
            timestamp=time.time(),
            session_id=session_id,
            provider=self.provider,
            direction=direction,
            text=clean_text,
            metadata=metadata or {},
        )
        self._transcripts.append(event)
        if direction == "input":
            self._last_input_transcript = clean_text
        elif direction == "output":
            self._last_output_transcript = clean_text

    def get_call_transcript(self, session_id: str | None = None) -> dict:
        target_session = session_id or self._last_session_id or self._session_id
        events = [
            item.to_dict()
            for item in self._transcripts
            if not target_session or item.session_id == target_session
        ]
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": target_session or None,
            "events": events,
        }

    def get_last_call_summary(self, session_id: str | None = None) -> dict:
        transcript = self.get_call_transcript(session_id)
        events = transcript["events"]
        input_events = [event for event in events if event["direction"] == "input"]
        output_events = [event for event in events if event["direction"] == "output"]
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": transcript["session_id"],
            "event_count": len(events),
            "input_event_count": len(input_events),
            "output_event_count": len(output_events),
            "first_event_at": events[0]["timestamp"] if events else None,
            "last_event_at": events[-1]["timestamp"] if events else None,
            "last_input_transcript": input_events[-1]["text"] if input_events else None,
            "last_output_transcript": output_events[-1]["text"] if output_events else None,
        }

    def _remove_queued_request(self, request_id: str) -> LiveAIRequest | None:
        found: LiveAIRequest | None = None
        remaining: list[LiveAIRequest] = []
        while True:
            try:
                request = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            if request.request_id == request_id and found is None:
                found = request
            else:
                remaining.append(request)
        for request in remaining:
            self._requests.put_nowait(request)
        return found

    def _task_done(self, task: asyncio.Task) -> None:
        if self._task is task and task.cancelled():
            return
        if self._task is task:
            self._task = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                self._last_error = str(exc)

    async def _run_provider(self, stream_url: str, initial_context: str) -> None:
        raise NotImplementedError

    async def _send_provider_text(self, text: str) -> None:
        raise NotImplementedError

    async def _submit_provider_result(
        self,
        request: LiveAIRequest,
        result: str,
        *,
        speak_to_caller: bool,
    ) -> None:
        raise NotImplementedError

    async def _close_provider_session(self) -> None:
        return None
