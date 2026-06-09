"""Hermes gateway adapter for HFP phone calls.

The adapter keeps Hermes' gateway API text-oriented while the HFP MCP server
owns Bluetooth and realtime PCM audio. Incoming call audio is segmented with a
small RMS VAD, transcribed through Hermes STT, and passed into the normal
gateway message pipeline. Outbound Hermes text is synthesized through Hermes
TTS, converted to 8 kHz signed 16-bit mono PCM, and written to the HFP audio
WebSocket.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import os
import subprocess
import tempfile
import time
import uuid
import wave
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional
from urllib import request

log = logging.getLogger(__name__)

try:
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource
except Exception:  # pragma: no cover - lets local repo tests import the plugin.
    Platform = None  # type: ignore
    PlatformConfig = object  # type: ignore
    BasePlatformAdapter = object  # type: ignore
    MessageEvent = None  # type: ignore
    MessageType = None  # type: ignore
    SendResult = None  # type: ignore
    SessionSource = None  # type: ignore


PCM_SAMPLE_RATE = 8000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
DEFAULT_STATUS_URL = "http://127.0.0.1:8001/status"
DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_CALL_TIMEOUT_SECONDS = 45.0
DEFAULT_IDLE_HANGUP_SECONDS = 20.0
HOME_CHAT_ALIASES = {"", "home", "hfp-phone", "hfp_phone", "owner"}
ROLE_ADMIN = "admin"
ROLE_TRUSTED = "trusted"
ROLE_UNKNOWN = "unknown"
CALL_SESSION_STATES = {"incoming", "dialing", "ringing", "active", "ending"}
VOICE_MODE_CLASSIC = "classic"
VOICE_MODE_GEMINI = "gemini_live"
VOICE_MODE_AUTO = "auto"
VOICE_MODES = {VOICE_MODE_CLASSIC, VOICE_MODE_GEMINI, VOICE_MODE_AUTO}


class HFPCallNoLongerActive(RuntimeError):
    """Raised when a gateway reply arrives after the phone call/audio stream ended."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_voice_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_")
    return mode if mode in VOICE_MODES else VOICE_MODE_CLASSIC


def _split_csv(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _caller_aliases(value: str) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    aliases = {raw}
    if _looks_like_phone_number(raw):
        aliases.add(_normalize_phone_target(raw))
    return aliases


def _classify_caller_role(
    caller: str,
    admin_callers: set[str],
    trusted_callers: set[str],
) -> str:
    aliases = _caller_aliases(caller)
    if aliases & admin_callers:
        return ROLE_ADMIN
    if aliases & trusted_callers:
        return ROLE_TRUSTED
    return ROLE_UNKNOWN


def _format_gemini_request(request_item: dict) -> str:
    name = str(request_item.get("name") or "gemini_request")
    args = request_item.get("arguments") if isinstance(request_item.get("arguments"), dict) else {}
    if name in {"ask_mcp_client", "ask_hermes"}:
        task = str(args.get("task") or "").strip()
        context = str(args.get("context") or "").strip()
        return f"Gemini Live asks MCP client: {task}" + (f"\nContext: {context}" if context else "")
    if name in {"get_mcp_client_context", "get_hermes_context"}:
        return f"Gemini Live requests MCP client context: {args.get('topic') or ''}".strip()
    if name in {"handoff_to_mcp_client", "handoff_to_hermes"}:
        return f"Gemini Live requests MCP client handoff: {args.get('reason') or ''}".strip()
    if name in {"notify_mcp_client", "notify_hermes"}:
        return f"Gemini Live notification: {args.get('event') or ''}".strip()
    return f"Gemini Live request {name}: {json.dumps(args, sort_keys=True)}"


def _looks_like_phone_number(value: str) -> bool:
    cleaned = value.strip().removeprefix("tel:")
    digits = [ch for ch in cleaned if ch.isdigit()]
    return len(digits) >= 3 and all(
        ch.isdigit() or ch in "+- ()." for ch in cleaned
    )


def _normalize_phone_target(value: str) -> str:
    value = value.strip().removeprefix("tel:").strip()
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


def _strip_hfp_target_prefix(value: str) -> str:
    raw = value.strip()
    lower = raw.lower()
    for prefix in ("hfp_phone:", "hfp-phone:"):
        if lower.startswith(prefix):
            return raw[len(prefix):].strip()
    return raw


def _is_hfp_call_session_id(value: str) -> bool:
    raw = value.strip()
    lower = raw.lower()
    if not lower.startswith(("hfp-phone:", "hfp_phone:")):
        return False
    candidate = _strip_hfp_target_prefix(raw)
    return not _looks_like_phone_number(candidate)


def _rms_s16le(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    count = len(frame) // 2
    total = 0
    for i in range(0, len(frame) - 1, 2):
        sample = int.from_bytes(frame[i:i + 2], "little", signed=True)
        total += sample * sample
    return math.sqrt(total / count)


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(PCM_CHANNELS)
        wav.setsampwidth(PCM_SAMPLE_WIDTH)
        wav.setframerate(PCM_SAMPLE_RATE)
        wav.writeframes(pcm)


def _read_json_url(url: str, timeout: float = 2.0) -> dict:
    with request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


async def _read_json_url_async(url: str, timeout: float = 2.0) -> dict:
    return await asyncio.to_thread(_read_json_url, url, timeout)


class HFPControlClient:
    """Small per-call MCP client for hfp-mcp streamable-http tools."""

    def __init__(self, mcp_url: str) -> None:
        self.mcp_url = mcp_url

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except Exception as exc:
            return {"ok": False, "error": f"MCP client unavailable: {exc}"}

        try:
            async with streamablehttp_client(self.mcp_url) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments or {})
            return self._coerce_tool_result(result)
        except Exception as exc:
            return {"ok": False, "error": f"MCP tool {name} failed: {exc}"}

    @staticmethod
    def _coerce_tool_result(result) -> dict:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None) or []
        for item in content:
            text = getattr(item, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"ok": True, "text": text}
        return {"ok": True}


class HFPPhoneAdapter(BasePlatformAdapter):
    """Hermes platform adapter for one active HFP call at a time."""

    name = "hfp_phone"
    SUPPORTS_MESSAGE_EDITING = False
    enforces_own_access_policy = True

    def __init__(self, config: PlatformConfig):
        if Platform is None:
            raise RuntimeError("Hermes gateway modules are not available")
        super().__init__(config, Platform("hfp_phone"))
        extra = getattr(config, "extra", {}) or {}
        self.mcp_url = os.getenv("HFP_PHONE_MCP_URL") or extra.get("mcp_url") or DEFAULT_MCP_URL
        self.status_url = (
            os.getenv("HFP_PHONE_STATUS_URL")
            or extra.get("status_url")
            or DEFAULT_STATUS_URL
        )
        self.default_address = (
            os.getenv("HFP_PHONE_DEFAULT_ADDRESS")
            or extra.get("default_address")
            or ""
        )
        self.owner_number = (
            os.getenv("HFP_PHONE_OWNER_NUMBER")
            or extra.get("owner_number")
            or ""
        )
        self.home_channel = (
            os.getenv("HFP_PHONE_HOME_CHANNEL")
            or extra.get("home_channel")
            or self.owner_number
            or "hfp-phone"
        )
        self.session_id = os.getenv("HFP_PHONE_SESSION_ID") or extra.get("session_id") or "active-call"
        self.voice_mode = _normalize_voice_mode(
            os.getenv("HFP_PHONE_VOICE_MODE") or extra.get("voice_mode")
        )
        self.auto_answer = _truthy(os.getenv("HFP_PHONE_AUTO_ANSWER") or extra.get("auto_answer"))
        self.auto_hangup_idle_seconds = float(
            extra.get(
                "auto_hangup_idle_seconds",
                os.getenv(
                    "HFP_PHONE_AUTO_HANGUP_IDLE_SECONDS",
                    str(DEFAULT_IDLE_HANGUP_SECONDS),
                ),
            )
        )
        self.call_timeout_seconds = float(
            extra.get(
                "call_timeout_seconds",
                os.getenv("HFP_PHONE_CALL_TIMEOUT_SECONDS", str(DEFAULT_CALL_TIMEOUT_SECONDS)),
            )
        )
        self.allowed_callers = _split_csv(
            os.getenv("HFP_PHONE_ALLOWED_CALLERS") or str(extra.get("allowed_callers") or "")
        )
        self.admin_callers = _split_csv(
            os.getenv("HFP_PHONE_ADMIN_CALLERS")
            or str(extra.get("admin_callers") or "")
        )
        if self.owner_number:
            self.admin_callers.update(_caller_aliases(self.owner_number))
        self.trusted_callers = _split_csv(
            os.getenv("HFP_PHONE_TRUSTED_CALLERS")
            or str(extra.get("trusted_callers") or "")
        )
        self.vad_threshold = float(extra.get("vad_threshold", os.getenv("HFP_PHONE_VAD_THRESHOLD", "150")))
        self.silence_seconds = float(extra.get("silence_seconds", os.getenv("HFP_PHONE_SILENCE_SECONDS", "0.8")))
        self.min_speech_seconds = float(extra.get("min_speech_seconds", os.getenv("HFP_PHONE_MIN_SPEECH_SECONDS", "0.3")))
        self._client = HFPControlClient(self.mcp_url)
        self._running = False
        self._watch_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._gemini_poll_task: Optional[asyncio.Task] = None
        self._ws = None
        self._active_chat_id = "hfp-phone"
        self._active_caller_id = "unknown"
        self._active_caller_role = ROLE_UNKNOWN
        self._active_call_id: Optional[str] = None
        self._last_state = "disconnected"
        self._idle_hangup_task: Optional[asyncio.Task] = None
        self._gemini_active = False
        self._gemini_fallback_to_classic = False
        self._pending_gemini_request_ids: deque[str] = deque()

    async def connect(self) -> bool:
        status = await self._client.call_tool("get_call_status")
        if status.get("ok") is False:
            log.error("HFP phone MCP server is unavailable: %s", status.get("error"))
            return False
        self._running = True
        self._watch_task = asyncio.create_task(self._watch_status(), name="hfp-phone-watch")
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._running = False
        for task in (
            self._watch_task,
            self._audio_task,
            self._gemini_poll_task,
            self._idle_hangup_task,
        ):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if not str(content or "").strip():
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        chat_id_str = str(chat_id or "")
        if self._should_send_via_gemini():
            return await self._send_via_gemini(chat_id_str, str(content))
        try:
            await self._ensure_outbound_call_for_send(chat_id_str)
            pcm = await _synthesize_pcm_for_call_async(str(content))
            await self._send_pcm(pcm)
            self._schedule_idle_hangup()
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        except HFPCallNoLongerActive as exc:
            # A spoken reply can arrive after the caller hung up or while the SCO
            # WebSocket is closing. Treat this as delivered/no-op for the gateway
            # so BasePlatformAdapter does not retry with a plain-text fallback
            # that would place a new call or speak stale diagnostic text.
            log.info("HFP phone reply dropped because call audio ended: %s", exc)
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        except Exception as exc:
            log.error("HFP phone send failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=False)

    def _voice_mode(self) -> str:
        return _normalize_voice_mode(getattr(self, "voice_mode", VOICE_MODE_CLASSIC))

    def _should_send_via_gemini(self) -> bool:
        if getattr(self, "_gemini_fallback_to_classic", False):
            return False
        mode = self._voice_mode()
        return mode == VOICE_MODE_GEMINI or (
            mode == VOICE_MODE_AUTO and bool(getattr(self, "_gemini_active", False))
        )

    async def _send_via_gemini(self, chat_id: str, content: str):
        try:
            await self._ensure_gemini_call_for_send(chat_id)
            request_id = self._pending_gemini_request_ids.popleft() if self._pending_gemini_request_ids else ""
            if request_id:
                result = await self._client.call_tool(
                    "submit_gemini_live_result",
                    {
                        "request_id": request_id,
                        "result": content,
                        "speak_to_caller": True,
                    },
                )
            else:
                result = await self._client.call_tool(
                    "send_gemini_live_text",
                    {
                        "text": content,
                        "urgency": "normal",
                        "speak_to_caller": True,
                    },
                )
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Gemini Live send failed")
            self._schedule_idle_hangup()
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        except HFPCallNoLongerActive as exc:
            log.info("HFP Gemini Live reply dropped because call ended: %s", exc)
            return SendResult(success=True, message_id=str(uuid.uuid4()))
        except Exception as exc:
            if self._voice_mode() == VOICE_MODE_AUTO:
                log.info("Gemini Live send failed in auto mode, falling back to classic: %s", exc)
                self._gemini_fallback_to_classic = True
                return await self.send(chat_id, content)
            log.error("HFP Gemini Live send failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=False)

    async def get_chat_info(self, chat_id):
        return {"name": "Bluetooth phone call", "type": "dm"}

    async def _watch_status(self) -> None:
        while self._running:
            try:
                status = await _read_json_url_async(self.status_url)
                await self._handle_status(status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("HFP status poll failed: %s", exc)
            await asyncio.sleep(1.0)

    async def _handle_status(self, status: dict) -> None:
        call_state = str(status.get("call_state") or "")
        connection = str(status.get("connection") or "")
        address = str(status.get("connected_address") or "unknown")
        if call_state in CALL_SESSION_STATES and self._active_call_id is None:
            self._active_call_id = uuid.uuid4().hex[:12]
        call_id = self._active_call_id or "idle"
        self._active_chat_id = f"hfp-phone:{address}:{call_id}"
        self._active_caller_id = address
        self._active_caller_role = _classify_caller_role(
            address,
            self.admin_callers,
            self.trusted_callers,
        )

        if call_state == "incoming" and self._last_state != "incoming":
            if self._can_answer(address):
                if self.auto_answer:
                    await self._client.call_tool("answer_call")
                else:
                    await self.handle_message(self._event(f"Incoming phone call from {address}"))

        if call_state == "active" and status.get("audio_active"):
            await self._start_call_voice()

        if connection == "disconnected" or call_state == "idle":
            await self._stop_call_voice()
            self._cancel_idle_hangup()
            self._active_call_id = None

        self._last_state = call_state

    def _can_answer(self, caller: str) -> bool:
        return not self.allowed_callers or caller in self.allowed_callers

    async def _start_call_voice(self) -> None:
        if self._voice_mode() == VOICE_MODE_CLASSIC or getattr(self, "_gemini_fallback_to_classic", False):
            if self._audio_task is None:
                await self._start_audio_stream()
            return

        result = await self._start_gemini_live()
        if result.get("ok"):
            return
        if self._voice_mode() == VOICE_MODE_AUTO:
            log.info("Gemini Live unavailable in auto mode, using classic HFP voice: %s", result.get("error"))
            self._gemini_fallback_to_classic = True
            if self._audio_task is None:
                await self._start_audio_stream()
            return
        log.warning("Gemini Live voice mode could not start: %s", result.get("error"))

    async def _stop_call_voice(self) -> None:
        await self._stop_gemini_live()
        await self._stop_audio_stream()
        self._gemini_fallback_to_classic = False
        self._pending_gemini_request_ids.clear()

    async def _start_gemini_live(self) -> dict:
        if getattr(self, "_gemini_active", False):
            return {"ok": True, "already_running": True}
        context = (
            f"Phone call session {getattr(self, '_active_call_id', '')}. "
            f"Caller identifier: {getattr(self, '_active_caller_id', 'unknown')}. "
            f"Caller role: {getattr(self, '_active_caller_role', ROLE_UNKNOWN)}."
        )
        result = await self._client.call_tool(
            "start_gemini_live_call",
            {
                "session_id": self.session_id,
                "initial_context": context,
            },
        )
        if not result.get("ok"):
            return result
        self._gemini_active = True
        if self._gemini_poll_task is None or self._gemini_poll_task.done():
            self._gemini_poll_task = asyncio.create_task(
                self._poll_gemini_live_requests(),
                name="hfp-phone-gemini-poll",
            )
        return result

    async def _stop_gemini_live(self) -> None:
        task = self._gemini_poll_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._gemini_poll_task is task:
                self._gemini_poll_task = None
        if getattr(self, "_gemini_active", False):
            result = await self._client.call_tool(
                "stop_gemini_live_call",
                {"reason": "call ended", "hangup_after": False},
            )
            if not result.get("ok"):
                log.debug("HFP stop_gemini_live_call failed: %s", result.get("error"))
        self._gemini_active = False

    async def _poll_gemini_live_requests(self) -> None:
        while self._running and getattr(self, "_gemini_active", False):
            try:
                result = await self._client.call_tool(
                    "poll_gemini_live_requests",
                    {"timeout_seconds": 5.0},
                )
                if not result.get("ok"):
                    await asyncio.sleep(1.0)
                    continue
                requests = result.get("requests") or []
                if not requests:
                    await asyncio.sleep(0.1)
                    continue
                for request_item in requests:
                    request_id = str(request_item.get("request_id") or "")
                    if request_id:
                        self._pending_gemini_request_ids.append(request_id)
                    await self.handle_message(self._event(_format_gemini_request(request_item)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Gemini Live request poll failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _start_audio_stream(self) -> None:
        if self._audio_task is not None and not self._audio_task.done():
            return
        result = await self._client.call_tool("ensure_audio_stream", {"session_id": self.session_id})
        if not result.get("ok"):
            log.warning("Could not start HFP audio stream: %s", result.get("error"))
            return
        stream_url = result.get("stream_url")
        if not stream_url:
            log.warning("HFP audio stream result had no stream_url: %s", result)
            return
        self._audio_task = asyncio.create_task(
            self._run_audio_stream(stream_url),
            name="hfp-phone-audio",
        )
        self._audio_task.add_done_callback(self._audio_task_done)

    async def _wait_for_audio_ws(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while self._running and time.monotonic() < deadline:
            if self._ws is not None and not self._ws.closed:
                return True
            await asyncio.sleep(0.05)
        return self._ws is not None and not self._ws.closed

    async def _ensure_outbound_call_for_send(self, chat_id: str) -> None:
        if self._ws is not None and not self._ws.closed:
            return

        status = await _read_json_url_async(self.status_url)
        if status.get("call_state") == "active" and status.get("audio_active"):
            await self._start_audio_stream()
            if await self._wait_for_audio_ws():
                return
            raise HFPCallNoLongerActive("Call audio stream did not become ready")

        # hfp-phone:<address> is an in-call session identity, not an outbound
        # phone number. If the caller hung up before Hermes finished thinking,
        # drop the spoken reply instead of falling through to the home-channel
        # resolver and accidentally placing a fresh outbound call.
        if _is_hfp_call_session_id(chat_id):
            raise HFPCallNoLongerActive(
                f"Call is no longer active (state: {status.get('call_state') or 'unknown'})"
            )

        target = self._resolve_call_target(chat_id)
        if not target:
            raise RuntimeError(
                "No outbound phone target configured. Set HFP_PHONE_OWNER_NUMBER "
                "or send to a phone-number chat_id."
            )

        result = await self._client.call_tool(
            "dial_and_wait",
            {"number": target, "timeout_seconds": self.call_timeout_seconds},
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or f"Could not dial {target}")

        await self._start_audio_stream()
        if await self._wait_for_audio_ws():
            return
        raise RuntimeError("Call audio stream did not become ready")

    async def _ensure_gemini_call_for_send(self, chat_id: str) -> None:
        if getattr(self, "_gemini_active", False):
            return

        status = await _read_json_url_async(self.status_url)
        if status.get("call_state") == "active" and status.get("audio_active"):
            result = await self._start_gemini_live()
            if result.get("ok"):
                return
            raise RuntimeError(result.get("error") or "Could not start Gemini Live")

        if _is_hfp_call_session_id(chat_id):
            raise HFPCallNoLongerActive(
                f"Call is no longer active (state: {status.get('call_state') or 'unknown'})"
            )

        target = self._resolve_call_target(chat_id)
        if not target:
            raise RuntimeError(
                "No outbound phone target configured. Set HFP_PHONE_OWNER_NUMBER "
                "or send to a phone-number chat_id."
            )

        result = await self._client.call_tool(
            "dial_and_wait",
            {"number": target, "timeout_seconds": self.call_timeout_seconds},
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or f"Could not dial {target}")

        result = await self._start_gemini_live()
        if result.get("ok"):
            return
        raise RuntimeError(result.get("error") or "Could not start Gemini Live")

    def _resolve_call_target(self, chat_id: str) -> str:
        candidate = _strip_hfp_target_prefix(chat_id)
        if _looks_like_phone_number(candidate):
            return _normalize_phone_target(candidate)
        if chat_id.strip().lower() in HOME_CHAT_ALIASES and self.owner_number:
            return _normalize_phone_target(self.owner_number)
        if chat_id == self.home_channel and self.owner_number:
            return _normalize_phone_target(self.owner_number)
        if _looks_like_phone_number(self.home_channel):
            return _normalize_phone_target(self.home_channel)
        return ""

    def _schedule_idle_hangup(self) -> None:
        self._cancel_idle_hangup()
        if self.auto_hangup_idle_seconds <= 0:
            return
        self._idle_hangup_task = asyncio.create_task(
            self._hangup_after_idle(),
            name="hfp-phone-idle-hangup",
        )

    def _cancel_idle_hangup(self) -> None:
        if self._idle_hangup_task is not None:
            self._idle_hangup_task.cancel()
            self._idle_hangup_task = None

    async def _hangup_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.auto_hangup_idle_seconds)
            await self._client.call_tool("hangup")
        except asyncio.CancelledError:
            raise
        finally:
            if self._idle_hangup_task is asyncio.current_task():
                self._idle_hangup_task = None

    def _audio_task_done(self, task: asyncio.Task) -> None:
        if self._audio_task is task:
            self._audio_task = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.error(
                    "HFP phone audio stream task failed: %s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    async def _stop_audio_stream(self) -> None:
        if self._audio_task is not None:
            task = self._audio_task
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._audio_task is task:
                self._audio_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if getattr(self, "_client", None) is not None:
            result = await self._client.call_tool(
                "stop_audio_capture", {"session_id": self.session_id}
            )
            if not result.get("ok") and "No session" not in str(result.get("error")):
                log.debug("HFP stop_audio_capture failed: %s", result.get("error"))

    async def _run_audio_stream(self, stream_url: str) -> None:
        try:
            import aiohttp
        except Exception as exc:
            log.error("aiohttp is required for the HFP phone audio stream: %s", exc)
            return

        speech = bytearray()
        last_voice_at: Optional[float] = None
        speech_started_at: Optional[float] = None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(stream_url) as ws:
                    self._ws = ws
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            continue
                        frame = bytes(msg.data)
                        now = time.monotonic()
                        if _rms_s16le(frame) >= self.vad_threshold:
                            self._cancel_idle_hangup()
                            if speech_started_at is None:
                                speech_started_at = now
                            last_voice_at = now
                            speech.extend(frame)
                            continue
                        if speech_started_at is not None:
                            speech.extend(frame)
                            if last_voice_at and now - last_voice_at >= self.silence_seconds:
                                duration = now - speech_started_at
                                pcm = bytes(speech)
                                speech.clear()
                                last_voice_at = None
                                speech_started_at = None
                                if duration >= self.min_speech_seconds:
                                    await self._transcribe_and_dispatch(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("HFP phone audio stream failed: %s", exc, exc_info=True)
        finally:
            self._ws = None

    async def _send_pcm(self, pcm: bytes) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise HFPCallNoLongerActive("Call audio stream is not connected")
        frame_bytes = 640
        for offset in range(0, len(pcm), frame_bytes):
            if ws.closed or ws is not self._ws:
                raise HFPCallNoLongerActive("Call audio stream closed while sending")
            try:
                await ws.send_bytes(pcm[offset:offset + frame_bytes])
            except Exception as exc:
                raise HFPCallNoLongerActive(
                    f"Call audio stream closed while sending: {exc}"
                ) from exc
            await asyncio.sleep(0.04)

    async def _transcribe_and_dispatch(self, pcm: bytes) -> None:
        transcript = await asyncio.to_thread(_transcribe_pcm, pcm)
        if not transcript:
            return
        self._cancel_idle_hangup()
        await self.handle_message(self._event(transcript))

    def _event(self, text: str):
        source = SessionSource(
            platform=Platform("hfp_phone"),
            chat_id=self._active_chat_id,
            chat_name="Bluetooth phone call",
            chat_type="dm",
            user_id=self._active_chat_id,
            user_name="Phone caller",
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(uuid.uuid4()),
            raw_message={
                "source": "hfp-phone",
                "hfp_caller_id": getattr(self, "_active_caller_id", "unknown"),
                "hfp_role": getattr(self, "_active_caller_role", ROLE_UNKNOWN),
                "hfp_call_id": getattr(self, "_active_call_id", None),
            },
        )


def _transcribe_pcm(pcm: bytes) -> str:
    try:
        from tools.transcription_tools import transcribe_audio
    except Exception as exc:
        log.error("Hermes STT unavailable: %s", exc)
        return ""
    with tempfile.TemporaryDirectory(prefix="hfp-phone-stt-") as tmp:
        wav_path = Path(tmp) / "caller.wav"
        _write_wav(wav_path, pcm)
        result = transcribe_audio(str(wav_path))
    if not result.get("success"):
        log.debug("HFP phone STT failed: %s", result.get("error"))
        return ""
    return str(result.get("transcript") or "").strip()


def _synthesize_pcm_for_call(text: str) -> bytes:
    audio_path = _synthesize_audio_file(text)
    return _convert_audio_to_pcm(audio_path)


async def _synthesize_pcm_for_call_async(text: str) -> bytes:
    return await asyncio.to_thread(_synthesize_pcm_for_call, text)


async def _convert_audio_to_pcm_async(audio_path: Path) -> bytes:
    return await asyncio.to_thread(_convert_audio_to_pcm, audio_path)


def _synthesize_audio_file(text: str) -> Path:
    try:
        tts_tool = importlib.import_module("tools.tts_tool")
    except Exception as exc:
        raise RuntimeError(f"Hermes TTS unavailable: {exc}") from exc

    tts_fn = getattr(tts_tool, "text_to_speech_tool", None)
    if tts_fn is None:
        raise RuntimeError("Hermes TTS tool has no text_to_speech_tool function")

    raw = tts_fn(text=text)
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"TTS failed: {data}")
    path = Path(str(data.get("file_path") or ""))
    if not path.exists():
        raise RuntimeError(f"TTS output missing: {path}")
    return path


def _convert_audio_to_pcm(audio_path: Path) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True)
    return proc.stdout


def check_requirements() -> bool:
    """Hermes HFP mode requires an MCP endpoint for call control/audio setup."""
    return bool(os.getenv("HFP_PHONE_MCP_URL"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("HFP_PHONE_MCP_URL") or extra.get("mcp_url"))


def _env_enablement() -> dict | None:
    mcp_url = os.getenv("HFP_PHONE_MCP_URL", "").strip()
    status_url = os.getenv("HFP_PHONE_STATUS_URL", "").strip()
    owner_number = os.getenv("HFP_PHONE_OWNER_NUMBER", "").strip()
    home_channel = os.getenv("HFP_PHONE_HOME_CHANNEL", "").strip()
    voice_mode = os.getenv("HFP_PHONE_VOICE_MODE", "").strip()
    if not mcp_url:
        return None
    result = {
        "mcp_url": mcp_url or DEFAULT_MCP_URL,
        "status_url": status_url or DEFAULT_STATUS_URL,
        "owner_number": owner_number,
        "gateway_restart_notification": False,
        "home_channel": {
            "chat_id": home_channel or owner_number or "hfp-phone",
            "name": "HFP Phone",
        },
    }
    if voice_mode:
        result["voice_mode"] = _normalize_voice_mode(voice_mode)
    return result


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    source = {}
    top_level = yaml_cfg.get("hfp_phone")
    if isinstance(top_level, dict):
        source.update(top_level)
    if isinstance(platform_cfg, dict):
        source.update(platform_cfg)

    bridgeable = {
        "mcp_url",
        "status_url",
        "default_address",
        "owner_number",
        "home_channel",
        "auto_answer",
        "call_timeout_seconds",
        "auto_hangup_idle_seconds",
        "allowed_callers",
        "admin_callers",
        "trusted_callers",
        "session_id",
        "voice_mode",
        "gateway_restart_notification",
        "unauthorized_dm_behavior",
    }
    bridged = {key: source[key] for key in bridgeable if key in source}
    return bridged or None


def _config_value(pconfig, name: str, default: str = "") -> str:
    extra = getattr(pconfig, "extra", {}) or {}
    value = extra.get(name)
    if value is None:
        return default
    return str(value)


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
    audio_file=None,
    tail_ms=1000.0,
):
    mcp_url = (
        os.getenv("HFP_PHONE_MCP_URL")
        or _config_value(pconfig, "mcp_url")
        or DEFAULT_MCP_URL
    )
    owner_number = os.getenv("HFP_PHONE_OWNER_NUMBER") or _config_value(
        pconfig, "owner_number"
    )
    home_channel = os.getenv("HFP_PHONE_HOME_CHANNEL") or _config_value(
        pconfig, "home_channel", owner_number or "hfp-phone"
    )
    session_id = f"hfp-call-{uuid.uuid4().hex}"
    timeout = float(
        os.getenv("HFP_PHONE_CALL_TIMEOUT_SECONDS")
        or _config_value(pconfig, "call_timeout_seconds", str(DEFAULT_CALL_TIMEOUT_SECONDS))
    )
    idle_hangup = float(
        os.getenv("HFP_PHONE_AUTO_HANGUP_IDLE_SECONDS")
        or _config_value(pconfig, "auto_hangup_idle_seconds", str(DEFAULT_IDLE_HANGUP_SECONDS))
    )

    target = _resolve_standalone_target(str(chat_id or ""), owner_number, home_channel)
    if not target:
        return {"error": "No outbound phone target configured"}

    client = HFPControlClient(mcp_url)
    dialed = False
    opened_audio = False
    playback_attempted = False
    audio_path: Path | None = None
    local_audio_exists = False
    pcm: bytes | None = None

    try:
        if audio_file:
            audio_path = Path(str(audio_file)).expanduser()
            local_audio_exists = audio_path.exists() and audio_path.is_file()
        else:
            audio_path = await asyncio.to_thread(_synthesize_audio_file, str(message))
            local_audio_exists = True

        status = await client.call_tool("get_call_status")
        if status.get("ok") is False:
            return {"error": status.get("error") or "Could not read HFP call status"}

        if status.get("call_state") == "active" and status.get("audio_active"):
            pass
        elif status.get("call_state") in (None, "", "idle"):
            result = await client.call_tool(
                "dial_and_wait",
                {"number": target, "timeout_seconds": timeout},
            )
            if not result.get("ok"):
                return {"error": result.get("error") or f"Could not dial {target}"}
            dialed = True
        else:
            return {
                "error": f"Already in call state: {status.get('call_state') or 'unknown'}"
            }

        result = await client.call_tool(
            "play_audio_file",
            {
                "audio_file": str(audio_path),
                "session_id": session_id,
                "tail_ms": tail_ms,
            },
        )
        playback_attempted = True
        if result.get("ok"):
            opened_audio = True
        else:
            # If Hermes generated the file on a different host than the MCP
            # server, MCP cannot read that path. Fall back to streaming local
            # converted PCM over a unique one-shot session.
            error = str(result.get("error") or "")
            if "not found" not in error.lower() and "not a file" not in error.lower():
                if dialed:
                    await client.call_tool("hangup")
                    await client.call_tool("cleanup_audio_sessions")
                return {"error": error or "Could not play audio file"}
            if not local_audio_exists:
                if dialed:
                    await client.call_tool("hangup")
                    await client.call_tool("cleanup_audio_sessions")
                return {"error": error or f"Audio file not found: {audio_path}"}
            pcm = await _convert_audio_to_pcm_async(audio_path)
            stream = await client.call_tool(
                "ensure_audio_stream", {"session_id": session_id}
            )
            if not stream.get("ok"):
                if dialed:
                    await client.call_tool("hangup")
                    await client.call_tool("cleanup_audio_sessions")
                return {
                    "error": stream.get("error") or "Could not start call audio stream"
                }
            opened_audio = True
            await _send_pcm_to_stream_url(str(stream["stream_url"]), pcm, tail_ms=tail_ms)

        if dialed and idle_hangup > 0:
            await asyncio.sleep(idle_hangup)
            await client.call_tool("hangup")
        return {"success": True, "message_id": str(uuid.uuid4())}
    except Exception as exc:
        if dialed:
            await client.call_tool("hangup")
            await client.call_tool("cleanup_audio_sessions")
        return {"error": str(exc)}
    finally:
        if opened_audio or playback_attempted:
            await client.call_tool("stop_audio_capture", {"session_id": session_id})


async def _hfp_phone_call_tool(arguments: dict, task_id: str | None = None, **_kwargs) -> dict:
    number = str(arguments.get("number") or "").strip()
    message = str(arguments.get("message") or "").strip()
    audio_file = str(arguments.get("audio_file") or "").strip()
    tail_ms = float(arguments.get("tail_ms") or 1000.0)
    if not number:
        number = os.getenv("HFP_PHONE_OWNER_NUMBER", "").strip()
    if not number:
        return {"ok": False, "error": "Missing number and HFP_PHONE_OWNER_NUMBER"}
    if not message and not audio_file:
        return {"ok": False, "error": "Missing message or audio_file"}

    result = await _standalone_send(
        None,
        number,
        message,
        thread_id=None,
        media_files=None,
        force_document=False,
        audio_file=audio_file or None,
        tail_ms=tail_ms,
    )
    if result.get("success"):
        return {"ok": True, "message_id": result.get("message_id")}
    return {"ok": False, "error": result.get("error", "Call failed")}


def _resolve_standalone_target(chat_id: str, owner_number: str, home_channel: str) -> str:
    candidate = _strip_hfp_target_prefix(chat_id)
    if _looks_like_phone_number(candidate):
        return _normalize_phone_target(candidate)
    if chat_id.strip().lower() in HOME_CHAT_ALIASES and owner_number:
        return _normalize_phone_target(owner_number)
    if chat_id == home_channel and owner_number:
        return _normalize_phone_target(owner_number)
    if _looks_like_phone_number(home_channel):
        return _normalize_phone_target(home_channel)
    return ""


async def _send_pcm_to_stream_url(
    stream_url: str,
    pcm: bytes,
    *,
    tail_ms: float = 1000.0,
) -> None:
    try:
        import aiohttp
    except Exception as exc:
        raise RuntimeError(f"aiohttp is required for HFP phone audio: {exc}") from exc

    frame_bytes = 640
    tail = b"\x00" * max(0, int(PCM_SAMPLE_RATE * PCM_CHANNELS * 2 * tail_ms / 1000))
    payload = pcm + tail
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(stream_url) as ws:
            for offset in range(0, len(payload), frame_bytes):
                await ws.send_bytes(payload[offset:offset + frame_bytes])
                await asyncio.sleep(0.04)
            await asyncio.sleep(1.0)


def register(ctx) -> None:
    ctx.register_platform(
        name="hfp_phone",
        label="HFP Phone",
        adapter_factory=lambda cfg: HFPPhoneAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        required_env=["HFP_PHONE_MCP_URL"],
        cron_deliver_env_var="HFP_PHONE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="HFP_PHONE_ALLOWED_CALLERS",
        allow_all_env="HFP_PHONE_ALLOW_ALL_CALLERS",
        platform_hint=(
            "You are speaking over a cellular phone call. Keep responses brief, "
            "clear, and suitable for voice."
        ),
        install_hint="Install Hermes with messaging/voice extras and run hfp-mcp-server in streamable-http mode.",
        pii_safe=True,
    )
    ctx.register_tool(
        name="hfp_phone_call",
        toolset="hfp_phone",
        schema={
            "type": "object",
            "properties": {
                "number": {
                    "type": "string",
                    "description": "Phone number to dial. Defaults to HFP_PHONE_OWNER_NUMBER.",
                },
                "message": {
                    "type": "string",
                    "description": "Reminder or instruction text to speak after the call connects.",
                },
                "audio_file": {
                    "type": "string",
                    "description": "Server- or Hermes-local audio file to play over the call.",
                },
                "tail_ms": {
                    "type": "number",
                    "description": "Silence tail to append after playback, in milliseconds.",
                },
            },
            "anyOf": [{"required": ["message"]}, {"required": ["audio_file"]}],
        },
        handler=_hfp_phone_call_tool,
        is_async=True,
    )
