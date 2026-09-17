"""
Realtime WebSocket sidecar for HFP call audio.

MCP tool calls remain the control plane. This sidecar carries binary PCM frames:

  server -> client: caller audio, 8 kHz s16le mono
  client -> server: playback/TTS audio, 8 kHz s16le mono

Gemini-owned streams may additionally send ordered JSON text controls named
``playback_start`` and ``playback_end``.  Other owners remain binary-only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import secrets
import socket
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import uvicorn
from starlette.applications import Starlette
from starlette.endpoints import WebSocketEndpoint
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .sco import (
    AudioManager,
    SCOPlaybackBufferOverflow,
    SCOPlaybackProtocolError,
    STREAM_FRAME_BYTES,
)
from ..config import (
    AUDIO_CHANNELS,
    AUDIO_DTYPE,
    AUDIO_SAMPLE_RATE,
    AUDIO_STREAM_FRAME_MS,
)

log = logging.getLogger(__name__)
TOKEN_TTL_SECONDS = 60.0
MAX_AUDIO_MESSAGE_BYTES = 64 * 1024
MAX_PLAYBACK_CONTROL_CHARS = 512
MAX_UTTERANCE_ID_CHARS = 128
PLAYBACK_CONTROL_TYPES = {"playback_start", "playback_end"}


def _parse_playback_control(data: str) -> tuple[str, str | None]:
    """Validate one bounded, extensible playback control object."""

    if not data or len(data) > MAX_PLAYBACK_CONTROL_CHARS:
        raise ValueError("invalid playback control size")
    try:
        payload = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid playback control JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") not in PLAYBACK_CONTROL_TYPES:
        raise ValueError("unsupported playback control")
    utterance_id = payload.get("utterance_id")
    if utterance_id is not None and (
        not isinstance(utterance_id, str)
        or not utterance_id
        or len(utterance_id) > MAX_UTTERANCE_ID_CHARS
    ):
        raise ValueError("invalid playback utterance id")
    return str(payload["type"]), utterance_id


@dataclass(frozen=True)
class StreamGrant:
    expires_at: float
    call_id: str | None
    server_instance_id: str | None
    generation: int | None
    owner: str | None


class AudioStreamServer:
    """Small authenticated WebSocket server for active SCO audio sessions."""

    def __init__(
        self,
        manager: AudioManager,
        host: str,
        port: int,
        public_host: str | None = None,
        *,
        secure: bool = False,
        embedded: bool = False,
        grant_validator: Callable[[str, StreamGrant], bool] | None = None,
        public_base_url: str | None = None,
        on_client_connected: Callable[[str], None] | None = None,
        on_client_disconnected: Callable[[str], None] | None = None,
    ) -> None:
        self._manager = manager
        self.host = host
        self.port = port
        self._public_host = public_host
        self._secure = secure
        self._embedded = embedded
        self._grant_validator = grant_validator
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._on_client_connected = on_client_connected
        self._on_client_disconnected = on_client_disconnected
        self._tokens: dict[str, dict[str, StreamGrant]] = {}
        self._tokens_lock = threading.Lock()
        self._attached: dict[str, int] = {}
        self._websockets: dict[str, tuple[WebSocket, asyncio.AbstractEventLoop]] = {}
        self._attached_lock = threading.Lock()
        self._started = threading.Event()
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def public_host(self) -> str:
        if self._public_host:
            return self._public_host
        if self.host in ("0.0.0.0", "::"):
            hostname = socket.getfqdn()
            if not hostname or hostname == "localhost":
                hostname = socket.gethostname()
            return hostname if hostname and hostname != "localhost" else "localhost"
        return self.host

    def start(self) -> None:
        """Start the sidecar in a background thread. Idempotent."""
        if self._started.is_set():
            return
        if self._embedded:
            self._started.set()
            return
        app = self._make_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="audio-stream-ws",
        )
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._server.started:
                self._started.set()
                break
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        if not self._started.is_set():
            self.stop()
            raise RuntimeError(f"audio WebSocket failed to bind {self.host}:{self.port}")
        log.info("Audio stream sidecar listening on ws://%s:%d", self.host, self.port)

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._server = None
        self._thread = None
        self._started.clear()

    def issue_token(
        self,
        session_id: str,
        *,
        call_id: str | None = None,
        server_instance_id: str | None = None,
        generation: int | None = None,
        owner: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self._tokens_lock:
            self._purge_expired_tokens_locked()
            # Reissuing a grant revokes every older unconsumed token for the
            # stream so retries cannot accumulate parallel capabilities.
            self._tokens[session_id] = {token: StreamGrant(
                expires_at=time.monotonic() + TOKEN_TTL_SECONDS,
                call_id=call_id,
                server_instance_id=server_instance_id,
                generation=generation,
                owner=owner,
            )}
        return token

    def stream_url(self, session_id: str, token: str) -> str:
        if self._public_base_url:
            parsed = urlsplit(self._public_base_url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            base = urlunsplit((scheme, parsed.netloc, "", "", ""))
            return f"{base}/audio/{session_id}?token={token}"
        return (
            f"{'wss' if self._secure else 'ws'}://{self.public_host}:{self.port}/audio/{session_id}"
            f"?token={token}"
        )

    def metadata(self) -> dict:
        return {
            "encoding": "pcm_s16le",
            "sample_rate_hz": AUDIO_SAMPLE_RATE,
            "channels": AUDIO_CHANNELS,
            "dtype": AUDIO_DTYPE,
            "frame_ms": AUDIO_STREAM_FRAME_MS,
            "frame_bytes": STREAM_FRAME_BYTES,
        }

    def health(self) -> dict:
        return {
            "listening": self._started.is_set(),
            "bind_host": self.host,
            "public_host": self.public_host,
            "port": self.port,
            "public_base_url": self._public_base_url,
        }

    def client_attached(self, session_id: str) -> bool:
        with self._attached_lock:
            return session_id in self._attached

    def detach_session(self, session_id: str) -> None:
        """Revoke a stream generation and actively close its client."""
        with self._tokens_lock:
            self._tokens.pop(session_id, None)
        with self._attached_lock:
            self._attached.pop(session_id, None)
            active = self._websockets.pop(session_id, None)
        if active is not None:
            websocket, loop = active
            if not loop.is_closed():
                asyncio.run_coroutine_threadsafe(websocket.close(code=1001), loop)

    def detach_all(self) -> None:
        with self._attached_lock:
            session_ids = set(self._attached) | set(self._websockets)
        with self._tokens_lock:
            session_ids.update(self._tokens)
        for session_id in session_ids:
            self.detach_session(session_id)

    def _consume_token(self, session_id: str, token: str) -> StreamGrant | None:
        with self._tokens_lock:
            self._purge_expired_tokens_locked()
            tokens = self._tokens.get(session_id)
            if not tokens:
                return None
            grant = tokens.pop(token, None)
            if not tokens:
                self._tokens.pop(session_id, None)
            return grant

    def _attach_client(
        self,
        session_id: str,
        client_id: int,
        websocket: WebSocket | None = None,
    ) -> bool:
        with self._attached_lock:
            if session_id in self._attached:
                return False
            self._attached[session_id] = client_id
            if websocket is not None:
                self._websockets[session_id] = (websocket, asyncio.get_running_loop())
        self._notify_client_callback(self._on_client_connected, session_id)
        return True

    def _detach_client(self, session_id: str, client_id: int) -> None:
        detached = False
        with self._attached_lock:
            if self._attached.get(session_id) == client_id:
                self._attached.pop(session_id, None)
                self._websockets.pop(session_id, None)
                detached = True
        if detached:
            self._notify_client_callback(self._on_client_disconnected, session_id)

    @staticmethod
    def _notify_client_callback(
        callback: Callable[[str], None] | None,
        session_id: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(session_id)
        except Exception as exc:
            log.warning("Audio client lifecycle callback failed for %s: %s", session_id, exc)

    def _purge_expired_tokens_locked(self) -> None:
        now = time.monotonic()
        empty_sessions = []
        for session_id, tokens in self._tokens.items():
            expired = [token for token, grant in tokens.items() if grant.expires_at <= now]
            for token in expired:
                tokens.pop(token, None)
            if not tokens:
                empty_sessions.append(session_id)
        for session_id in empty_sessions:
            self._tokens.pop(session_id, None)

    def _make_app(self) -> Starlette:
        owner = self

        class _AudioEndpoint(WebSocketEndpoint):
            encoding = None

            async def on_connect(self, websocket: WebSocket) -> None:
                session_id = websocket.path_params["session_id"]
                token = websocket.query_params.get("token", "")
                grant = owner._consume_token(session_id, token)
                if grant is None:
                    log.warning("Rejecting HFP audio WebSocket for %s: invalid token", session_id)
                    await websocket.close(code=1008)
                    return
                if owner._grant_validator is not None and not owner._grant_validator(
                    session_id, grant
                ):
                    log.warning("Rejecting HFP audio WebSocket for %s: stale grant", session_id)
                    await websocket.close(code=1008)
                    return
                if owner._manager.get_session(session_id) is None:
                    log.warning("Rejecting HFP audio WebSocket for %s: missing session", session_id)
                    await websocket.close(code=1008)
                    return
                client_id = id(websocket)
                if not owner._attach_client(session_id, client_id, websocket):
                    log.warning(
                        "Rejecting HFP audio WebSocket for %s: client already attached",
                        session_id,
                    )
                    await websocket.close(code=1008)
                    return
                await websocket.accept()
                websocket.state.session_id = session_id
                websocket.state.client_id = client_id
                websocket.state.stream_owner = grant.owner
                websocket.state.sender = asyncio.create_task(
                    owner._send_capture(websocket, session_id),
                    name=f"audio-ws-send-{session_id}",
                )

            async def on_receive(self, websocket: WebSocket, data) -> None:
                session_id = websocket.state.session_id
                session = owner._manager.get_session(session_id)
                if session is None:
                    await websocket.close(code=1001)
                    return
                if isinstance(data, str):
                    if websocket.state.stream_owner != "gemini_live":
                        await websocket.close(
                            code=1003,
                            reason="binary PCM required",
                        )
                        return
                    try:
                        control_type, utterance_id = _parse_playback_control(data)
                        if control_type == "playback_start":
                            session.start_buffered_playback(utterance_id)
                        else:
                            session.end_buffered_playback(utterance_id)
                    except (RuntimeError, SCOPlaybackProtocolError, ValueError):
                        await websocket.close(
                            code=1008,
                            reason="invalid playback control",
                        )
                    return
                if not isinstance(data, bytes):
                    await websocket.close(code=1003, reason="binary PCM required")
                    return
                if not data or len(data) > MAX_AUDIO_MESSAGE_BYTES or len(data) % 2:
                    await websocket.close(code=1009, reason="invalid PCM frame size")
                    return
                try:
                    session.queue_playback(data)
                except SCOPlaybackProtocolError:
                    await websocket.close(
                        code=1008,
                        reason="playback_start required",
                    )
                    return
                except SCOPlaybackBufferOverflow:
                    log.error(
                        "Closing HFP audio stream %s: playback buffer overflow",
                        session_id,
                    )
                    await websocket.close(
                        code=1011,
                        reason="playback buffer overflow",
                    )
                    return

            async def on_disconnect(self, websocket: WebSocket, close_code: int) -> None:
                sender = getattr(websocket.state, "sender", None)
                if sender is not None:
                    sender.cancel()
                session_id = getattr(websocket.state, "session_id", None)
                client_id = getattr(websocket.state, "client_id", None)
                if session_id is not None and client_id is not None:
                    owner._detach_client(session_id, client_id)

        return Starlette(routes=[WebSocketRoute("/audio/{session_id}", _AudioEndpoint)])

    def routes(self) -> list[WebSocketRoute]:
        """Return routes for embedding the audio plane in the control ASGI app."""
        return list(self._make_app().routes)

    async def _send_capture(self, websocket: WebSocket, session_id: str) -> None:
        try:
            while True:
                session = self._manager.get_session(session_id)
                if session is None:
                    await websocket.close(code=1001)
                    return
                frame = session.pop_stream_frame()
                if frame is None:
                    await asyncio.sleep(0.005)
                    continue
                await websocket.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError):
            return
        except asyncio.CancelledError:
            raise
