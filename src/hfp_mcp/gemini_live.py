"""Optional Gemini Live call bridge for the HFP MCP server.

The module is deliberately lazy: importing it must not require google-genai or
aiohttp. Those dependencies are checked only when Gemini Live is enabled.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_SEND_RATE = 16000
GEMINI_RECEIVE_RATE = 24000
HFP_RATE = 8000
PCM_WIDTH = 2
CHANNELS = 1
HFP_FRAME_BYTES = 640


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def gemini_live_enabled() -> bool:
    return _truthy(os.getenv("HFP_GEMINI_LIVE_ENABLED"))


def gemini_api_key() -> str:
    return (
        os.getenv("HFP_GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def gemini_live_model() -> str:
    return os.getenv("HFP_GEMINI_LIVE_MODEL", "").strip() or DEFAULT_MODEL


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def availability() -> dict:
    if not gemini_live_enabled():
        return {"available": False, "reason": "disabled"}
    if not gemini_api_key():
        return {"available": False, "reason": "missing_api_key"}
    if not _module_available("google.genai"):
        return {"available": False, "reason": "missing_google_genai"}
    if not _module_available("aiohttp"):
        return {"available": False, "reason": "missing_aiohttp"}
    return {"available": True, "reason": "ok", "model": gemini_live_model()}


@dataclass
class GeminiRequest:
    request_id: str
    function_call_id: str
    name: str
    arguments: dict
    created_at: float

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "name": self.name,
            "arguments": self.arguments,
            "created_at": self.created_at,
        }


class PcmResampler:
    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._source_pos = 0.0

    def convert(self, pcm: bytes) -> bytes:
        if not pcm or self.src_rate == self.dst_rate:
            return pcm
        samples = _pcm_to_samples(pcm)
        if not samples:
            return b""
        step = self.src_rate / self.dst_rate
        pos = self._source_pos
        out: list[int] = []
        while pos < len(samples):
            out.append(samples[int(pos)])
            pos += step
        self._source_pos = pos - len(samples)
        return _samples_to_pcm(out)


EnsureStream = Callable[[str], Awaitable[dict]]
ClearPlayback = Callable[[str], Awaitable[dict]]
HangupCall = Callable[[], Awaitable[dict]]


class GeminiLiveManager:
    """Owns at most one Gemini Live call session."""

    def __init__(
        self,
        *,
        ensure_stream: EnsureStream,
        clear_playback: ClearPlayback,
        hangup: HangupCall,
    ) -> None:
        self._ensure_stream = ensure_stream
        self._clear_playback = clear_playback
        self._hangup = hangup
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._ws = None
        self._live_session = None
        self._session_id = ""
        self._started_at: float | None = None
        self._last_error = ""
        self._last_input_transcript = ""
        self._last_output_transcript = ""
        self._requests: asyncio.Queue[GeminiRequest] = asyncio.Queue()
        self._pending: dict[str, GeminiRequest] = {}

    def status(self) -> dict:
        avail = availability()
        running = self._task is not None and not self._task.done()
        return {
            "ok": True,
            "available": bool(avail.get("available")),
            "availability_reason": avail.get("reason"),
            "model": gemini_live_model(),
            "running": running,
            "session_id": self._session_id or None,
            "uptime_seconds": (
                max(0.0, time.monotonic() - self._started_at)
                if self._started_at is not None and running
                else None
            ),
            "pending_requests": len(self._pending) + self._requests.qsize(),
            "last_error": self._last_error or None,
            "last_input_transcript": self._last_input_transcript or None,
            "last_output_transcript": self._last_output_transcript or None,
        }

    def pending_requests(self) -> dict:
        """Return Gemini tool calls that have been polled but not answered.

        poll_requests() moves tool calls from the live queue into _pending so a
        client can answer them later with submit_result(). Generic MCP clients
        may disconnect, crash, or poll in one process and submit from another;
        this method makes those outstanding request IDs recoverable without
        depending on any specific client implementation.
        """
        return {
            "ok": True,
            "requests": [item.to_dict() for item in self._pending.values()],
        }

    async def start(self, session_id: str = "active-call", initial_context: str | None = None) -> dict:
        avail = availability()
        if not avail.get("available"):
            return {"ok": False, "error": avail.get("reason") or "gemini_unavailable"}
        if self._task is not None and not self._task.done():
            return {"ok": True, "session_id": self._session_id, "already_running": True}

        stream = await self._ensure_stream(session_id)
        if not stream.get("ok"):
            return stream
        stream_url = stream.get("client_stream_url") or stream.get("stream_url")
        if not stream_url:
            return {"ok": False, "error": "audio_stream_url_missing"}

        self._stop_event = asyncio.Event()
        self._session_id = session_id
        self._started_at = time.monotonic()
        self._last_error = ""
        self._task = asyncio.create_task(
            self._run(stream_url, initial_context or ""),
            name=f"gemini-live-{session_id}",
        )
        self._task.add_done_callback(self._task_done)
        return {"ok": True, "session_id": session_id, "model": gemini_live_model()}

    async def stop(self, reason: str | None = None, hangup: bool = False) -> dict:
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
        self._live_session = None
        if self._session_id:
            await self._clear_playback(self._session_id)
        if hangup:
            await self._hangup()
        session_id, self._session_id = self._session_id, ""
        self._started_at = None
        return {"ok": True, "session_id": session_id or None, "reason": reason}

    async def send_text(
        self,
        text: str,
        *,
        urgency: str = "normal",
        speak_to_caller: bool = True,
    ) -> dict:
        if not text.strip():
            return {"ok": False, "error": "empty_text"}
        session = self._live_session
        if session is None:
            return {"ok": False, "error": "gemini_live_not_running"}
        if urgency == "urgent" and self._session_id:
            await self._clear_playback(self._session_id)
        prefix = ""
        if not speak_to_caller:
            prefix = "Internal context update for the assistant. Do not speak this verbatim: "
        await session.send_realtime_input(text=f"{prefix}{text}")
        return {"ok": True}

    async def poll_requests(self, timeout_seconds: float = 5.0) -> dict:
        timeout = max(0.0, float(timeout_seconds))
        requests: list[dict] = []
        try:
            first = await asyncio.wait_for(self._requests.get(), timeout=timeout)
            self._pending[first.request_id] = first
            requests.append(first.to_dict())
        except asyncio.TimeoutError:
            return {"ok": True, "requests": []}

        while True:
            try:
                item = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
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
        request = self._pending.pop(request_id, None)
        if request is None:
            return {"ok": False, "error": "request_not_found"}
        session = self._live_session
        if session is None:
            return {"ok": False, "error": "gemini_live_not_running"}

        from google.genai import types

        function_response = types.FunctionResponse(
            id=request.function_call_id,
            name=request.name,
            response={
                "result": result,
                "speak_to_caller": bool(speak_to_caller),
            },
        )
        await session.send_tool_response(function_responses=[function_response])
        return {"ok": True}

    def _task_done(self, task: asyncio.Task) -> None:
        if self._task is task and task.cancelled():
            return
        if self._task is task:
            self._task = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                self._last_error = str(exc)
                log.error("Gemini Live session failed: %s", exc, exc_info=True)

    async def _run(self, stream_url: str, initial_context: str) -> None:
        import aiohttp
        from google import genai

        client = genai.Client(api_key=gemini_api_key())
        system_instruction = (
            "You are speaking on a cellular phone call through a Bluetooth "
            "hands-free gateway. Keep spoken responses brief and natural. "
            "Use the connected MCP client or orchestrator for tasks, context, "
            "permissions, reminders, or actions outside the call."
        )
        if initial_context.strip():
            # Initial context is operator/developer context, not something to speak
            # verbatim to the caller. Put it in the Live config instruction instead
            # of sending it as realtime user input, otherwise Gemini may read the
            # instructions out loud at call start.
            system_instruction = f"{system_instruction}\n\nCall context/instructions:\n{initial_context.strip()}"
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": system_instruction,
            "tools": [{"function_declarations": _function_declarations()}],
        }

        async with client.aio.live.connect(model=gemini_live_model(), config=config) as live_session:
            self._live_session = live_session
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(stream_url) as ws:
                    self._ws = ws
                    sender = asyncio.create_task(
                        self._hfp_to_gemini(ws, live_session),
                        name="hfp-to-gemini",
                    )
                    receiver = asyncio.create_task(
                        self._gemini_to_hfp(ws, live_session),
                        name="gemini-to-hfp",
                    )
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        task.result()

    async def _hfp_to_gemini(self, ws, live_session) -> None:
        import aiohttp
        from google.genai import types

        resampler = PcmResampler(HFP_RATE, GEMINI_SEND_RATE)
        async for msg in ws:
            if self._stop_event.is_set():
                return
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            pcm = resampler.convert(bytes(msg.data))
            await live_session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={GEMINI_SEND_RATE}")
            )

    async def _gemini_to_hfp(self, ws, live_session) -> None:
        resampler = PcmResampler(GEMINI_RECEIVE_RATE, HFP_RATE)
        # google-genai's Live receive() iterator is turn-scoped: it can finish
        # after one model response even though the underlying session should stay
        # open for the next caller utterance. Keep opening receive iterators until
        # the call bridge is explicitly stopped or an actual websocket/session
        # error is raised.
        while not self._stop_event.is_set():
            saw_response = False
            async for response in live_session.receive():
                saw_response = True
                if self._stop_event.is_set():
                    return
                await self._handle_tool_calls(response)
                content = getattr(response, "server_content", None)
                if content is None:
                    continue
                input_tx = getattr(content, "input_transcription", None)
                output_tx = getattr(content, "output_transcription", None)
                if input_tx is not None and getattr(input_tx, "text", None):
                    self._last_input_transcript = input_tx.text
                if output_tx is not None and getattr(output_tx, "text", None):
                    self._last_output_transcript = output_tx.text
                if getattr(content, "interrupted", False) and self._session_id:
                    await self._clear_playback(self._session_id)
                model_turn = getattr(content, "model_turn", None)
                parts = getattr(model_turn, "parts", None) or []
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    if inline is None:
                        continue
                    audio = _inline_data_bytes(getattr(inline, "data", b""))
                    pcm = resampler.convert(audio)
                    await _send_hfp_pcm(ws, pcm)
            if not saw_response:
                await asyncio.sleep(0.05)

    async def _handle_tool_calls(self, response) -> None:
        tool_call = getattr(response, "tool_call", None)
        function_calls = getattr(tool_call, "function_calls", None) or []
        for fc in function_calls:
            request_id = uuid.uuid4().hex
            item = GeminiRequest(
                request_id=request_id,
                function_call_id=str(getattr(fc, "id", "") or request_id),
                name=str(getattr(fc, "name", "") or "unknown"),
                arguments=dict(getattr(fc, "args", None) or {}),
                created_at=time.time(),
            )
            await self._requests.put(item)


async def _send_hfp_pcm(ws, pcm: bytes) -> None:
    for offset in range(0, len(pcm), HFP_FRAME_BYTES):
        await ws.send_bytes(pcm[offset:offset + HFP_FRAME_BYTES])
        await asyncio.sleep(0.04)


def _inline_data_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return base64.b64decode(data)
    return bytes(data or b"")


def _pcm_to_samples(pcm: bytes) -> list[int]:
    end = len(pcm) - (len(pcm) % 2)
    return [
        int.from_bytes(pcm[index:index + 2], "little", signed=True)
        for index in range(0, end, 2)
    ]


def _samples_to_pcm(samples: list[int]) -> bytes:
    out = bytearray()
    for sample in samples:
        out.extend(max(-32768, min(32767, sample)).to_bytes(2, "little", signed=True))
    return bytes(out)


def _function_declarations() -> list[dict]:
    def ask_tool(name: str, client_label: str) -> dict:
        return {
            "name": name,
            "description": f"Ask the connected {client_label} to perform a task, fetch data, or use tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "context": {"type": "string"},
                    "urgency": {"type": "string"},
                },
                "required": ["task"],
            },
        }

    def notify_tool(name: str, client_label: str) -> dict:
        return {
            "name": name,
            "description": f"Notify the connected {client_label} about call events, transcript snippets, or outcomes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "transcript": {"type": "string"},
                    "caller_id": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["event"],
            },
        }

    def context_tool(name: str, client_label: str) -> dict:
        return {
            "name": name,
            "description": f"Fetch relevant context from the connected {client_label}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "caller_id": {"type": "string"},
                },
                "required": ["topic"],
            },
        }

    def handoff_tool(name: str, client_label: str) -> dict:
        return {
            "name": name,
            "description": f"Ask the connected {client_label} to take over an unclear, privileged, or long-running workflow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "transcript": {"type": "string"},
                },
                "required": ["reason"],
            },
        }

    return [
        ask_tool("ask_mcp_client", "MCP client or orchestrator"),
        notify_tool("notify_mcp_client", "MCP client or orchestrator"),
        context_tool("get_mcp_client_context", "MCP client or orchestrator"),
        handoff_tool("handoff_to_mcp_client", "MCP client or orchestrator"),
    ]
