"""Provider-neutral lifecycle and request state for live AI phone calls.

The provider implementation owns the network/media tasks, while this module
owns the externally visible lifecycle, bounded transcript history, and the
idempotent function-call queue used by MCP/Hermes integrations.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Optional


EnsureStream = Callable[[str], Awaitable[dict]]
ClearPlayback = Callable[[str], Awaitable[dict]]
HangupCall = Callable[[], Awaitable[dict]]
Availability = Callable[[], dict]
ModelName = Callable[[], str]


class LiveAIState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    FAILED = "failed"


TERMINAL_REQUEST_STATES = {
    "completed",
    "denied",
    "cancelled",
    "expired",
    "stale",
}


@dataclass
class LiveAIRequest:
    request_id: str
    function_call_id: str
    name: str
    arguments: dict[str, Any]
    created_at: float
    session_id: str = ""
    provider: str = "live_ai"
    state: str = "queued"
    session_active: bool = True
    stale: bool = False
    stale_reason: str | None = None
    deadline_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "function_call_id": self.function_call_id,
            "name": self.name,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "session_id": self.session_id or None,
            "provider": self.provider,
            "state": self.state,
            "session_active": self.session_active,
            "stale": self.stale,
        }
        if self.deadline_at is not None:
            payload["deadline_at"] = self.deadline_at
        if self.stale_reason:
            payload["stale_reason"] = self.stale_reason
        return payload

    def mark_pending(self) -> None:
        if self.state not in TERMINAL_REQUEST_STATES:
            self.state = "pending"

    def mark_completed(self) -> None:
        self.state = "completed"
        self.session_active = False

    def mark_expired(self, reason: str = "deadline_expired") -> None:
        self.state = "expired"
        self.session_active = False
        self.stale = True
        self.stale_reason = reason or "deadline_expired"

    def mark_stale(self, reason: str) -> None:
        normalized = reason or "stale"
        self.state = "cancelled" if "cancel" in normalized else "stale"
        self.session_active = False
        self.stale = True
        self.stale_reason = normalized


@dataclass
class TranscriptEvent:
    timestamp: float
    session_id: str
    provider: str
    direction: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "provider": self.provider,
            "direction": self.direction,
            "text": self.text,
            "metadata": self.metadata,
        }


class LiveAIManager:
    """Own exactly one provider-backed live AI call session.

    A provider must call :meth:`_mark_ready` only after its media and provider
    connections are usable. This prevents API clients from receiving a false
    positive from ``start`` and immediately losing their first audio/tool turn.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: ModelName,
        availability: Availability,
        ensure_stream: EnsureStream,
        clear_playback: ClearPlayback,
        hangup: HangupCall,
        startup_timeout_seconds: float = 15.0,
        request_queue_size: int = 128,
        transcript_event_limit: int = 256,
        transcript_text_limit: int = 4096,
        stale_request_limit: int = 256,
        full_transcripts_enabled: bool = True,
        transcript_sink=None,
        memory_sink=None,
    ) -> None:
        self.provider = provider
        self._model = model
        self._availability = availability
        self._ensure_stream = ensure_stream
        self._clear_playback = clear_playback
        self._hangup = hangup
        self._startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self._transcript_text_limit = max(256, int(transcript_text_limit))
        self._stale_request_limit = max(16, int(stale_request_limit))
        self._pending_request_limit = max(1, int(request_queue_size))
        self._full_transcripts_enabled = bool(full_transcripts_enabled)
        self._transcript_sink = transcript_sink
        self._memory_sink = memory_sink
        self._transcript_storage_error = None
        self._memory_capture_error = None

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._ready_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._state = LiveAIState.STOPPED
        self._state_changed_at = time.monotonic()
        self._ws: Any = None
        self._stream_info: dict[str, Any] = {}
        self._session_id = ""
        self._last_session_id = ""
        self._started_at: float | None = None
        self._last_error = ""
        self._startup_error = ""
        self._last_input_transcript = ""
        self._last_output_transcript = ""

        self._requests: asyncio.Queue[LiveAIRequest] = asyncio.Queue(
            maxsize=max(1, int(request_queue_size))
        )
        self._pending: OrderedDict[str, LiveAIRequest] = OrderedDict()
        self._stale: OrderedDict[str, LiveAIRequest] = OrderedDict()
        self._known_request_states: OrderedDict[str, str] = OrderedDict()
        self._function_call_ids: dict[str, str] = {}
        self._dropped_requests = 0

        self._transcripts: deque[TranscriptEvent] = deque(
            maxlen=max(8, int(transcript_event_limit))
        )
        self._transcript_fragments: dict[str, str] = {"input": "", "output": ""}
        self._transcript_fragment_times = {}
        self._truncated_transcripts = 0
        # A single bounded caller utterance may be retained in memory long
        # enough to create the operator-approved redacted call summary.  It is
        # never returned by transcript/status APIs and is consumed at call end.
        self._summary_fragment = ""
        self._summary_candidate = ""
        self._summary_candidate_session_id = ""
        self._summary_candidate_limit = 600

    @property
    def lifecycle_state(self) -> LiveAIState:
        return self._state

    def status(self, call_context: dict | None = None) -> dict[str, Any]:
        avail = self._availability()
        running = self.running
        queued = self._requests.qsize()
        pending = len(self._pending)
        stale = len(self._stale)
        call_context = call_context or {}
        call_state = call_context.get("call_state")
        call_active = bool(call_context.get("call_active"))
        audio_active = bool(call_context.get("audio_active"))
        active_session = self._session_id or self._last_session_id
        active_session_has_stale_request = any(
            request.session_id == active_session for request in self._stale.values()
        )
        return {
            "ok": True,
            "provider": self.provider,
            "available": bool(avail.get("available")),
            "availability_reason": avail.get("reason"),
            "model": self._model(),
            "state": self._state.value,
            "running": running,
            "ready": self._state is LiveAIState.RUNNING and self._ready_event.is_set(),
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
            "dropped_requests": self._dropped_requests,
            "truncated_transcripts": self._truncated_transcripts,
            "transcript_events": len(self._transcripts),
            "call_state": call_state,
            "call_active": call_active,
            "audio_active": audio_active,
            "session_stale": active_session_has_stale_request or (
                bool(self._last_session_id) and not running and not call_active
            ),
            "last_error": self._last_error or None,
            "transcript_redacted": not self._full_transcripts_enabled,
            "transcript_storage_error": self._transcript_storage_error,
            "memory_capture_error": self._memory_capture_error,
            "last_input_transcript": (
                self._last_input_transcript or None
                if self._full_transcripts_enabled
                else None
            ),
            "last_output_transcript": (
                self._last_output_transcript or None
                if self._full_transcripts_enabled
                else None
            ),
        }

    @property
    def running(self) -> bool:
        return (
            self._state
            in {LiveAIState.STARTING, LiveAIState.RUNNING, LiveAIState.RECONNECTING}
            and self._task is not None
            and not self._task.done()
        )

    def signal_stop(self, reason: str = "session_stopped") -> None:
        """Synchronously mark externally initiated teardown as intentional.

        Call/media teardown can close the provider's audio WebSocket before the
        asynchronous :meth:`stop` coroutine gets CPU time.  Setting the stop
        signal first lets provider tasks distinguish that expected close from a
        transport failure.  The caller must still schedule :meth:`stop` to
        perform the full cleanup.
        """

        self._stop_requested = True
        self._stop_event.set()

    def pending_requests(self) -> dict[str, Any]:
        return {
            "ok": True,
            "requests": [item.to_dict() for item in self._pending.values()],
        }

    def stale_requests(self) -> dict[str, Any]:
        return {
            "ok": True,
            "requests": [item.to_dict() for item in self._stale.values()],
        }

    async def start(
        self,
        session_id: str = "active-call",
        initial_context: str | None = None,
    ) -> dict[str, Any]:
        async with self._lifecycle_lock:
            self._stop_requested = False
            avail = await self._check_availability()
            if not avail.get("available"):
                return {
                    "ok": False,
                    "error": avail.get("reason") or "live_ai_unavailable",
                }
            if self.running:
                if session_id != self._session_id:
                    return {
                        "ok": False,
                        "error": "live_ai_session_conflict",
                        "session_id": self._session_id,
                    }
                result = {
                    "ok": True,
                    "provider": self.provider,
                    "session_id": self._session_id,
                    "state": self._state.value,
                    "already_running": True,
                    "initial_context_applied": False,
                    "initial_context_delivery": "not_requested",
                }
                context = str(initial_context or "").strip()
                if not context:
                    return result

                # Setup/system instructions cannot be mutated after a Live
                # session has connected. Deliver late context through the
                # provider's realtime-text path instead of silently dropping
                # it on an idempotent second start. This is internal context,
                # not a request to recite the text verbatim.
                delivered = await self.send_text(
                    context,
                    urgency="normal",
                    speak_to_caller=False,
                )
                if not delivered.get("ok"):
                    return {
                        **result,
                        "ok": False,
                        "error": "live_ai_context_update_failed",
                        "context_error": delivered.get("error")
                        or "provider rejected realtime context",
                        "initial_context_delivery": "failed",
                    }
                return {
                    **result,
                    "initial_context_applied": True,
                    "initial_context_delivery": "realtime_text",
                }

            try:
                await self._prepare_provider()
            except Exception as exc:
                self._set_state(LiveAIState.FAILED, str(exc))
                return {"ok": False, "error": f"provider_prepare_failed: {exc}"}
            if self._stop_requested:
                self._set_state(LiveAIState.STOPPED)
                return {"ok": False, "error": "live_ai_start_cancelled"}

            try:
                stream = await self._ensure_stream(session_id)
            except Exception as exc:
                self._set_state(LiveAIState.FAILED, str(exc))
                return {"ok": False, "error": f"audio_stream_failed: {exc}"}
            if not stream.get("ok"):
                self._set_state(
                    LiveAIState.FAILED,
                    str(stream.get("error") or "audio_stream_failed"),
                )
                return stream
            stream_result = stream.get("result")
            stream_details = stream_result if isinstance(stream_result, dict) else stream
            self._stream_info = {**stream, **stream_details}
            await self._on_stream_acquired()
            if self._stop_requested:
                with contextlib.suppress(Exception):
                    await self._close_provider_session()
                self._stream_info = {}
                self._set_state(LiveAIState.STOPPED)
                return {"ok": False, "error": "live_ai_start_cancelled"}
            stream_url = stream_details.get("client_stream_url") or stream_details.get(
                "stream_url"
            )
            if not stream_url:
                self._set_state(LiveAIState.FAILED, "audio_stream_url_missing")
                with contextlib.suppress(Exception):
                    await self._close_provider_session()
                self._stream_info = {}
                return {"ok": False, "error": "audio_stream_url_missing"}

            self._stop_event = asyncio.Event()
            self._ready_event = asyncio.Event()
            if session_id != self._last_session_id:
                # Gemini function-call IDs are scoped to a Live session. A
                # provider may legitimately reuse an ID after session rotation.
                self._known_request_states.clear()
                self._function_call_ids.clear()
                self._summary_fragment = ""
                self._summary_candidate = ""
                self._summary_candidate_session_id = ""
            self._session_id = session_id
            self._last_session_id = session_id
            self._started_at = time.monotonic()
            self._last_error = ""
            self._startup_error = ""
            self._set_state(LiveAIState.STARTING)
            self._task = asyncio.create_task(
                self._run_provider(str(stream_url), initial_context or ""),
                name=f"{self.provider}-live-{session_id}",
            )
            self._task.add_done_callback(self._task_done)

            try:
                await asyncio.wait_for(
                    self._ready_event.wait(), timeout=self._startup_timeout_seconds
                )
            except asyncio.TimeoutError:
                self._last_error = "live_ai_start_timeout"
                await self._shutdown_provider_task()
                self._set_state(LiveAIState.FAILED, self._last_error)
                return {"ok": False, "error": self._last_error}

            if self._state is not LiveAIState.RUNNING:
                error = self._startup_error or self._last_error or "live_ai_start_failed"
                await self._shutdown_provider_task()
                self._set_state(LiveAIState.FAILED, error)
                return {"ok": False, "error": error}

            return {
                "ok": True,
                "provider": self.provider,
                "session_id": session_id,
                "model": self._model(),
                "state": self._state.value,
                "ready": True,
                "initial_context_applied": bool(
                    str(initial_context or "").strip()
                ),
                "initial_context_delivery": (
                    "setup_system_instruction"
                    if str(initial_context or "").strip()
                    else "not_requested"
                ),
            }

    async def _check_availability(self) -> dict[str, Any]:
        return self._availability()

    async def _prepare_provider(self) -> None:
        """Finish provider-specific cold-start work before call audio begins."""

        return None

    async def stop(self, reason: str | None = None, hangup: bool = False) -> dict[str, Any]:
        # Signal and cancel before taking the lifecycle lock so a concurrent
        # start waiting for provider readiness cannot block stop for its entire
        # startup timeout.
        self.signal_stop(reason or "session_stopped")
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        async with self._lifecycle_lock:
            stop_reason = reason or "session_stopped"
            session_id = self._session_id
            if self._state is LiveAIState.STOPPED and self._task is None:
                return {
                    "ok": True,
                    "provider": self.provider,
                    "session_id": session_id or self._last_session_id or None,
                    "reason": reason,
                    "already_stopped": True,
                }

            self._set_state(LiveAIState.STOPPING)
            self.flush_transcript_fragments()
            self.mark_session_stale(stop_reason)
            self._stop_event.set()
            await self._shutdown_provider_task()
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
            with contextlib.suppress(Exception):
                await self._close_provider_session()
            if session_id:
                with contextlib.suppress(Exception):
                    await self._clear_playback(self._media_stream_id(session_id))
            if hangup:
                with contextlib.suppress(Exception):
                    await self._hangup()
            self._session_id = ""
            self._stream_info = {}
            self._started_at = None
            self._set_state(LiveAIState.STOPPED)
            return {
                "ok": True,
                "provider": self.provider,
                "session_id": session_id or None,
                "reason": reason,
                "state": self._state.value,
            }

    async def send_text(
        self,
        text: str,
        *,
        urgency: str = "normal",
        speak_to_caller: bool = False,
    ) -> dict[str, Any]:
        if not text.strip():
            return {"ok": False, "error": "empty_text"}
        if len(text) > 16384:
            return {"ok": False, "error": "text_too_long"}
        if self._state is LiveAIState.RECONNECTING:
            return {"ok": False, "error": "live_ai_reconnecting", "retryable": True}
        if self._state is not LiveAIState.RUNNING or not self.running:
            return {"ok": False, "error": "live_ai_not_running"}
        if urgency == "urgent" and self._session_id:
            await self._clear_playback(self._media_stream_id(self._session_id))
        prefix = ""
        if not speak_to_caller:
            prefix = "Internal context update for the assistant. Do not speak this verbatim: "
        try:
            await self._send_provider_text(f"{prefix}{text}")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "provider": self.provider}

    async def speak(self, text: str, *, urgency: str = "normal") -> dict[str, Any]:
        return await self.send_text(text, urgency=urgency, speak_to_caller=True)

    async def poll_requests(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        timeout = min(30.0, max(0.0, float(timeout_seconds)))
        requests: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(self._requests.get(), timeout=timeout)
            self._move_pending(first)
            requests.append(first.to_dict())
        except asyncio.TimeoutError:
            return {"ok": True, "requests": []}

        while True:
            try:
                item = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._move_pending(item)
            requests.append(item.to_dict())
        return {"ok": True, "requests": requests}

    async def submit_result(
        self,
        request_id: str,
        result: str | dict[str, Any],
        *,
        speak_to_caller: bool = True,
    ) -> dict[str, Any]:
        request = self._pending.get(request_id)
        if request is None:
            if request_id in self._stale:
                return {"ok": False, "error": "request_stale"}
            if self._known_request_states.get(request_id) == "completed":
                return {
                    "ok": True,
                    "provider": self.provider,
                    "request_id": request_id,
                    "already_submitted": True,
                }
            return {"ok": False, "error": "request_not_found"}
        if self._state is not LiveAIState.RUNNING or not self.running:
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
        request.mark_completed()
        self._function_call_ids.pop(request.function_call_id, None)
        self._remember_request_state(request_id, "completed")
        return {"ok": True, "provider": self.provider, "request_id": request_id}

    def cancel_request(self, request_id: str, reason: str = "cancelled") -> dict[str, Any]:
        request = self._pending.pop(request_id, None)
        if request is None:
            request = self._remove_queued_request(request_id)
        if request is None:
            if request_id in self._stale:
                return {"ok": True, "request_id": request_id, "already_stale": True}
            return {"ok": False, "error": "request_not_found"}
        self._mark_request_stale(request, reason or "cancelled")
        return {"ok": True, "request_id": request_id, "reason": request.stale_reason}

    def cancel_function_calls(
        self, function_call_ids: list[str], reason: str = "provider_cancelled"
    ) -> list[str]:
        cancelled: list[str] = []
        for function_call_id in function_call_ids:
            request_id = self._function_call_ids.get(str(function_call_id), str(function_call_id))
            result = self.cancel_request(request_id, reason)
            if result.get("ok"):
                cancelled.append(request_id)
        return cancelled

    def clear_requests(
        self,
        session_id: str | None = None,
        *,
        only_stale: bool = True,
    ) -> dict[str, Any]:
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
            self._mark_request_stale(request, reason)
        for request_id, request in list(self._pending.items()):
            self._pending.pop(request_id, None)
            self._mark_request_stale(request, reason)

    def add_request(self, request: LiveAIRequest) -> bool:
        if not request.session_id:
            request.session_id = self._session_id or self._last_session_id
        request.provider = self.provider
        request.request_id = str(request.request_id or request.function_call_id)
        request.function_call_id = str(request.function_call_id or request.request_id)
        if not request.request_id:
            return False
        if request.request_id in self._known_request_states:
            return False
        self._remember_request_state(request.request_id, "queued")
        self._function_call_ids[request.function_call_id] = request.request_id
        if self._requests.full():
            self._mark_request_stale(request, "queue_overflow")
            self._dropped_requests += 1
            return False
        self._requests.put_nowait(request)
        return True

    def append_transcript_fragment(
        self,
        direction: str,
        text: str,
        *,
        final: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if direction not in self._transcript_fragments:
            direction = str(direction or "unknown")
            self._transcript_fragments.setdefault(direction, "")
        fragment = str(text or "")
        if fragment:
            self._transcript_fragment_times.setdefault(direction, time.time())
            if direction == "input":
                self._summary_fragment = self._merge_summary_fragment(
                    self._summary_fragment,
                    fragment,
                )
            combined = (
                self._merge_transcript_fragment(
                    self._transcript_fragments[direction], fragment
                )
                if self._full_transcripts_enabled or self._memory_sink
                else "[redacted]"
            )
            # Keep the current utterance intact until finalized. Providers may
            # send cumulative revisions; splitting here would duplicate prefixes.
            # add_transcript persists the full event before bounding display text.
            self._transcript_fragments[direction] = combined
        if final:
            self.flush_transcript_fragment(direction, metadata=metadata)

    def flush_transcript_fragment(
        self, direction: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        text = self._transcript_fragments.get(direction, "")
        self._transcript_fragments[direction] = ""
        if direction == "input":
            candidate = self._summary_fragment
            self._summary_fragment = ""
            self._consider_summary_candidate(candidate)
        self.add_transcript(direction, text, metadata=metadata, timestamp=self._transcript_fragment_times.pop(direction, None))

    def flush_transcript_fragments(self) -> None:
        for direction in list(self._transcript_fragments):
            self.flush_transcript_fragment(direction)

    def add_transcript(
        self,
        direction: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        # Both consumers see the same stable event before display truncation.
        import uuid
        event = {"event_id": uuid.uuid4().hex, "timestamp": time.time() if timestamp is None else timestamp,
                 "session_id": self._session_id or self._last_session_id or "unknown",
                 "provider": self.provider, "direction": direction,
                 "text": clean_text, "metadata": metadata or {}}
        if self._memory_sink:
            try:
                self._memory_sink(event)
            except Exception as exc:
                self._memory_capture_error = type(exc).__name__
        if self._full_transcripts_enabled and self._transcript_sink:
            try:
                self._transcript_sink(event)
            except Exception as exc:
                self._transcript_storage_error = type(exc).__name__
        if len(clean_text) > self._transcript_text_limit:
            clean_text = clean_text[-self._transcript_text_limit :]
            self._truncated_transcripts += 1
        if direction == "input" and clean_text != "[redacted]":
            self._consider_summary_candidate(clean_text)
        session_id = self._session_id or self._last_session_id or "unknown"
        retained_text = clean_text if self._full_transcripts_enabled else "[redacted]"
        event = TranscriptEvent(
            timestamp=event["timestamp"],
            session_id=session_id,
            provider=self.provider,
            direction=direction,
            text=retained_text,
            metadata=metadata or {},
        )
        self._transcripts.append(event)
        if direction == "input" and self._full_transcripts_enabled:
            self._last_input_transcript = clean_text
        elif direction == "output" and self._full_transcripts_enabled:
            self._last_output_transcript = clean_text

    def consume_summary_candidate(self, session_id: str | None = None) -> str | None:
        """Consume the private, bounded caller-message candidate for a call."""
        target = session_id or self._last_session_id or self._session_id
        if (
            not self._summary_candidate
            or (target and target != self._summary_candidate_session_id)
        ):
            return None
        candidate = self._summary_candidate
        self._summary_candidate = ""
        self._summary_candidate_session_id = ""
        return candidate

    def _consider_summary_candidate(self, value: str) -> None:
        candidate = " ".join(str(value or "").replace("\x00", " ").split())
        if not candidate or candidate == "[redacted]":
            return
        candidate = candidate[: self._summary_candidate_limit]
        # Favor a substantive message over acknowledgements and partial turns.
        if len(candidate) > len(self._summary_candidate):
            self._summary_candidate = candidate
            self._summary_candidate_session_id = (
                self._session_id or self._last_session_id or "unknown"
            )

    def _merge_summary_fragment(self, current: str, fragment: str) -> str:
        """Bound incremental or cumulative transcription fragments."""
        fragment = str(fragment or "")
        if not fragment:
            return current
        if fragment.startswith(current):
            merged = fragment
        elif current.endswith(fragment):
            merged = current
        else:
            merged = current + fragment
        return merged[-self._summary_candidate_limit :]

    @staticmethod
    def _merge_transcript_fragment(current: str, fragment: str) -> str:
        """Merge either incremental or cumulative provider transcription."""

        if not current:
            return fragment
        if fragment.startswith(current):
            return fragment
        if current.endswith(fragment):
            return current
        return current + fragment

    def get_call_transcript(self, session_id: str | None = None) -> dict[str, Any]:
        target_session = session_id or self._last_session_id or self._session_id
        if not self._full_transcripts_enabled:
            return {
                "ok": False,
                "provider": self.provider,
                "session_id": target_session or None,
                "error": "full_transcripts_disabled",
                "events": [],
            }
        events = self._transcript_events(target_session)
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": target_session or None,
            "events": events,
        }

    def _transcript_events(self, target_session: str | None) -> list[dict[str, Any]]:
        events = [
            item.to_dict()
            for item in self._transcripts
            if not target_session or item.session_id == target_session
        ]
        return events

    def get_last_call_summary(self, session_id: str | None = None) -> dict[str, Any]:
        target_session = session_id or self._last_session_id or self._session_id
        events = self._transcript_events(target_session)
        input_events = [event for event in events if event["direction"] == "input"]
        output_events = [event for event in events if event["direction"] == "output"]
        return {
            "ok": True,
            "provider": self.provider,
            "session_id": target_session or None,
            "event_count": len(events),
            "input_event_count": len(input_events),
            "output_event_count": len(output_events),
            "first_event_at": events[0]["timestamp"] if events else None,
            "last_event_at": events[-1]["timestamp"] if events else None,
            "redacted": not self._full_transcripts_enabled,
            "last_input_transcript": (
                input_events[-1]["text"]
                if input_events and self._full_transcripts_enabled
                else None
            ),
            "last_output_transcript": (
                output_events[-1]["text"]
                if output_events and self._full_transcripts_enabled
                else None
            ),
        }

    def _move_pending(self, request: LiveAIRequest) -> None:
        while len(self._pending) >= self._pending_request_limit:
            _, oldest = self._pending.popitem(last=False)
            self._mark_request_stale(oldest, "pending_overflow")
            self._dropped_requests += 1
        request.mark_pending()
        self._pending[request.request_id] = request
        self._remember_request_state(request.request_id, "pending")

    def _mark_request_stale(self, request: LiveAIRequest, reason: str) -> None:
        request.mark_stale(reason)
        self._function_call_ids.pop(request.function_call_id, None)
        self._stale[request.request_id] = request
        self._stale.move_to_end(request.request_id)
        self._remember_request_state(request.request_id, request.state)
        while len(self._stale) > self._stale_request_limit:
            self._stale.popitem(last=False)

    def _mark_request_expired(
        self, request: LiveAIRequest, reason: str = "deadline_expired"
    ) -> None:
        request.mark_expired(reason)
        self._function_call_ids.pop(request.function_call_id, None)
        self._stale[request.request_id] = request
        self._stale.move_to_end(request.request_id)
        self._remember_request_state(request.request_id, "expired")
        while len(self._stale) > self._stale_request_limit:
            self._stale.popitem(last=False)

    def _remember_request_state(self, request_id: str, state: str) -> None:
        self._known_request_states[request_id] = state
        self._known_request_states.move_to_end(request_id)
        while len(self._known_request_states) > self._stale_request_limit * 2:
            self._known_request_states.popitem(last=False)

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

    def _set_state(self, state: LiveAIState, error: str | None = None) -> None:
        self._state = state
        self._state_changed_at = time.monotonic()
        if error:
            self._last_error = error

    def _media_stream_id(self, fallback: str = "") -> str:
        return str(
            self._stream_info.get("stream_id")
            or self._stream_info.get("session_id")
            or fallback
        )

    def _mark_ready(self) -> None:
        if not self._stop_requested and not self._stop_event.is_set():
            self._set_state(LiveAIState.RUNNING)
        self._ready_event.set()

    def _mark_reconnecting(self, error: str | None = None) -> None:
        if not self._stop_requested and not self._stop_event.is_set():
            self._set_state(LiveAIState.RECONNECTING, error)

    def _mark_provider_start_failed(self, error: str) -> None:
        self._startup_error = error
        self._set_state(LiveAIState.FAILED, error)
        self._ready_event.set()

    async def _shutdown_provider_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _task_done(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
        exc = None if task.cancelled() else task.exception()
        if task.cancelled() or self._stop_requested or self._stop_event.is_set():
            if self._state is LiveAIState.STARTING:
                self._startup_error = "live_ai_start_cancelled"
                self._set_state(LiveAIState.FAILED, self._startup_error)
                self._ready_event.set()
            return
        if exc is None:
            error = "live_ai_provider_stopped"
        else:
            error = str(exc) or exc.__class__.__name__
        self._last_error = error
        self._startup_error = self._startup_error or error
        self._set_state(LiveAIState.FAILED, error)
        self._ready_event.set()

    async def _run_provider(self, stream_url: str, initial_context: str) -> None:
        raise NotImplementedError

    async def _on_stream_acquired(self) -> None:
        return None

    async def _send_provider_text(self, text: str) -> None:
        raise NotImplementedError

    async def _submit_provider_result(
        self,
        request: LiveAIRequest,
        result: str | dict[str, Any],
        *,
        speak_to_caller: bool,
    ) -> None:
        raise NotImplementedError

    async def _close_provider_session(self) -> None:
        return None
