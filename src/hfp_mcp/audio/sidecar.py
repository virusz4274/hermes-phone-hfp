"""
Realtime WebSocket sidecar for HFP call audio.

MCP tool calls remain the control plane. This sidecar carries binary PCM frames:

  server -> client: caller audio, 8 kHz s16le mono
  client -> server: playback/TTS audio, 8 kHz s16le mono
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from typing import Optional

import uvicorn
from starlette.applications import Starlette
from starlette.endpoints import WebSocketEndpoint
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .sco import AudioManager, STREAM_FRAME_BYTES
from ..config import (
    AUDIO_CHANNELS,
    AUDIO_DTYPE,
    AUDIO_SAMPLE_RATE,
    AUDIO_STREAM_FRAME_MS,
)

log = logging.getLogger(__name__)


class AudioStreamServer:
    """Small authenticated WebSocket server for active SCO audio sessions."""

    def __init__(
        self,
        manager: AudioManager,
        host: str,
        port: int,
        public_host: str | None = None,
    ) -> None:
        self._manager = manager
        self.host = host
        self.port = port
        self._public_host = public_host
        self._tokens: dict[str, str] = {}
        self._tokens_lock = threading.Lock()
        self._started = threading.Event()
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def public_host(self) -> str:
        if self._public_host:
            return self._public_host
        if self.host in ("0.0.0.0", "::"):
            return "127.0.0.1"
        return self.host

    def start(self) -> None:
        """Start the sidecar in a background thread. Idempotent."""
        if self._started.is_set():
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
        self._started.set()
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

    def issue_token(self, session_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._tokens_lock:
            self._tokens[session_id] = token
        return token

    def stream_url(self, session_id: str, token: str) -> str:
        return (
            f"ws://{self.public_host}:{self.port}/audio/{session_id}"
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

    def _token_ok(self, session_id: str, token: str) -> bool:
        with self._tokens_lock:
            return self._tokens.get(session_id) == token

    def _make_app(self) -> Starlette:
        owner = self

        class _AudioEndpoint(WebSocketEndpoint):
            encoding = None

            async def on_connect(self, websocket: WebSocket) -> None:
                session_id = websocket.path_params["session_id"]
                token = websocket.query_params.get("token", "")
                if not owner._token_ok(session_id, token):
                    await websocket.close(code=1008)
                    return
                if owner._manager.get_session(session_id) is None:
                    await websocket.close(code=1008)
                    return
                await websocket.accept()
                websocket.state.session_id = session_id
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
                if isinstance(data, bytes):
                    session.queue_playback(data)

            async def on_disconnect(self, websocket: WebSocket, close_code: int) -> None:
                sender = getattr(websocket.state, "sender", None)
                if sender is not None:
                    sender.cancel()

        return Starlette(routes=[WebSocketRoute("/audio/{session_id}", _AudioEndpoint)])

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
