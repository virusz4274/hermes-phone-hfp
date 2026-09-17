"""Bounded Gemini playback and interruption handling.

The manager owns lifecycle, locks, queue state and metrics. This mixin owns
playback ordering/pacing and calls the manager's media cleanup and timing hooks.
"""

from __future__ import annotations
import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

HFP_FRAME_BYTES = 320  # 20 ms, mono signed 16-bit PCM at 8 kHz.

PLAYBACK_BACKLOG_SECONDS = 60

PLAYBACK_QUEUE_FRAMES = PLAYBACK_BACKLOG_SECONDS * 50  # Fixed 20 ms frames.

PLAYBACK_QUEUE_ITEM_CAPACITY = PLAYBACK_QUEUE_FRAMES * 3

PLAYBACK_PREBUFFER_FRAMES = 8  # 160 ms protects SCO from event-loop jitter.

PLAYBACK_PREBUFFER_WAIT_SECONDS = 0.12

PLAYBACK_FRAME_SECONDS = 0.02

PLAYBACK_REBUFFER_LATE_SECONDS = 0.08


class _PlaybackBufferOverflow(RuntimeError):
    """Normal speech exceeded the finite, burst-tolerant playout reservoir."""


@dataclass(frozen=True, slots=True)
class _PlaybackItem:
    epoch: int
    kind: str
    window_id: str | None = None
    pcm: bytes = b""


class GeminiPlaybackMixin:
    async def _playback_writer(self, ws: Any) -> None:
        loop = asyncio.get_running_loop()
        next_send_at = loop.time()
        pending_item: _PlaybackItem | None = None
        while not self._stop_event.is_set():
            if pending_item is None:
                item = await self._dequeue_playback_item()
            else:
                item = pending_item
                pending_item = None
            if item.epoch != self._playback_epoch:
                next_send_at = loop.time()
                continue

            if item.kind == "playback_start":
                if not await self._send_playback_control(ws, item):
                    continue
                frames, pending_item = await self._collect_playback_batch(
                    epoch=item.epoch,
                    wait_seconds=PLAYBACK_PREBUFFER_WAIT_SECONDS,
                )
                if item.epoch != self._playback_epoch:
                    self._playback_frames_interrupted += len(frames)
                    if (
                        pending_item is not None
                        and pending_item.epoch != self._playback_epoch
                    ):
                        pending_item = None
                    next_send_at = loop.time()
                    continue
                if frames:
                    frame_count = len(frames)
                    if not await self._send_playback_bytes(
                        ws, item.epoch, b"".join(frames)
                    ):
                        self._playback_frames_interrupted += frame_count
                        continue
                    self._playback_frames_sent += frame_count
                    self._playback_prebuffer_events += 1
                    self._playback_prebuffer_frames += frame_count
                    next_send_at = loop.time() + PLAYBACK_FRAME_SECONDS
                if pending_item is not None and pending_item.kind == "playback_end":
                    await self._send_playback_control(ws, pending_item)
                    pending_item = None
                continue

            if item.kind == "playback_end":
                await self._send_playback_control(ws, item)
                next_send_at = loop.time()
                continue

            frame = item.pcm
            now = loop.time()
            if next_send_at > now:
                await asyncio.sleep(next_send_at - now)
            if item.epoch != self._playback_epoch:
                if item.kind == "audio":
                    self._playback_frames_interrupted += 1
                next_send_at = loop.time()
                continue

            now = loop.time()
            lateness = max(0.0, now - next_send_at)
            if lateness >= 0.0005:
                lateness_ms = lateness * 1000.0
                self._playback_late_frames += 1
                self._playback_lateness_total_ms += lateness_ms
                self._playback_lateness_max_ms = max(
                    self._playback_lateness_max_ms, lateness_ms
                )
            if lateness >= PLAYBACK_REBUFFER_LATE_SECONDS:
                frames, pending_item = await self._collect_playback_batch(
                    epoch=item.epoch,
                    initial_frame=frame,
                    wait_seconds=PLAYBACK_PREBUFFER_WAIT_SECONDS,
                )
                if item.epoch != self._playback_epoch:
                    self._playback_frames_interrupted += len(frames)
                    if (
                        pending_item is not None
                        and pending_item.epoch != self._playback_epoch
                    ):
                        pending_item = None
                    next_send_at = loop.time()
                    continue
                frame_count = len(frames)
                if not await self._send_playback_bytes(
                    ws, item.epoch, b"".join(frames)
                ):
                    self._playback_frames_interrupted += frame_count
                    continue
                self._playback_frames_sent += frame_count
                self._playback_rebuffer_events += 1
                self._playback_rebuffer_frames += frame_count
                next_send_at = loop.time() + PLAYBACK_FRAME_SECONDS
                if pending_item is not None and pending_item.kind == "playback_end":
                    await self._send_playback_control(ws, pending_item)
                    pending_item = None
                continue

            if not await self._send_playback_bytes(ws, item.epoch, frame):
                self._playback_frames_interrupted += 1
                continue
            self._playback_frames_sent += 1
            next_send_at += PLAYBACK_FRAME_SECONDS

    async def _dequeue_playback_item(self) -> _PlaybackItem:
        item = await self._playback_queue.get()
        if item.kind == "audio":
            self._playback_buffered_frames = max(0, self._playback_buffered_frames - 1)
        return item

    async def _collect_playback_batch(
        self,
        *,
        epoch: int,
        initial_frame: bytes | None = None,
        wait_seconds: float,
    ) -> tuple[list[bytes], _PlaybackItem | None]:
        """Collect a bounded safety lead while remaining interruption-aware."""

        frames = [initial_frame] if initial_frame is not None else []
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        while len(frames) < PLAYBACK_PREBUFFER_FRAMES:
            if epoch != self._playback_epoch or self._stop_event.is_set():
                return frames, None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(
                    self._dequeue_playback_item(), timeout=min(0.01, remaining)
                )
            except asyncio.TimeoutError:
                continue
            if item.epoch != epoch:
                return frames, item
            if item.kind != "audio":
                return frames, item
            frames.append(item.pcm)
        return frames, None

    async def _send_playback_control(self, ws: Any, item: _PlaybackItem) -> bool:
        payload: dict[str, str] = {"type": item.kind}
        if item.window_id:
            payload["utterance_id"] = item.window_id
        async with self._playback_send_lock:
            if item.epoch != self._playback_epoch or self._stop_event.is_set():
                return False
            await ws.send_str(json.dumps(payload, separators=(",", ":")))
        return True

    async def _send_playback_bytes(self, ws: Any, epoch: int, pcm: bytes) -> bool:
        async with self._playback_send_lock:
            if epoch != self._playback_epoch or self._stop_event.is_set():
                return False
            await ws.send_bytes(pcm)
            if pcm:
                self._mark_timing("first_audio_sent_to_hfp")
        return True

    async def _discard_playback(self, *, interrupted: bool) -> None:
        queued_frames = self._playback_buffered_frames
        if interrupted:
            self._playback_frames_interrupted += queued_frames
        self._playback_epoch += 1
        self._clear_playback_queue()
        self._playback_partial.clear()
        if not self._session_id:
            return
        playback_stream_id = self._media_stream_id(self._session_id)
        with contextlib.suppress(asyncio.TimeoutError, Exception):

            async def _clear_after_pending_send() -> None:
                async with self._playback_send_lock:
                    await self._clear_playback(playback_stream_id)

            await asyncio.wait_for(_clear_after_pending_send(), timeout=0.2)

    def _begin_playback_window(self) -> str:
        self._playback_window_sequence += 1
        window_id = f"{self._session_generation}-{self._playback_window_sequence}"
        self._enqueue_playback_control("playback_start", window_id)
        return window_id

    def _end_playback_window(self, window_id: str | None) -> None:
        if window_id:
            self._enqueue_playback_control("playback_end", window_id)

    def _enqueue_playback_control(self, kind: str, window_id: str) -> None:
        try:
            self._playback_queue.put_nowait(
                _PlaybackItem(
                    epoch=self._playback_epoch,
                    kind=kind,
                    window_id=window_id,
                )
            )
        except asyncio.QueueFull as exc:
            self._playback_overflow_events += 1
            raise _PlaybackBufferOverflow(
                "gemini_playback_control_backlog_exceeded"
            ) from exc

    def _enqueue_playback_pcm(
        self, pcm: bytes, *, window_id: str | None = None
    ) -> None:
        if not pcm:
            return
        combined = bytes(self._playback_partial) + bytes(pcm)
        frame_count = len(combined) // HFP_FRAME_BYTES
        item_available = (
            self._playback_queue.maxsize - self._playback_queue.qsize()
            if self._playback_queue.maxsize > 0
            else self._playback_frame_capacity
        )
        available = min(
            self._playback_frame_capacity - self._playback_buffered_frames,
            item_available,
        )
        if frame_count > available:
            rejected = max(1, (len(pcm) + HFP_FRAME_BYTES - 1) // HFP_FRAME_BYTES)
            self._dropped_playback_frames += rejected
            self._playback_overflow_events += 1
            raise _PlaybackBufferOverflow(
                "gemini_playback_backlog_exceeded: "
                f"needed {frame_count} frame(s), {available} available"
            )

        complete_bytes = frame_count * HFP_FRAME_BYTES
        for offset in range(0, complete_bytes, HFP_FRAME_BYTES):
            frame = combined[offset : offset + HFP_FRAME_BYTES]
            self._playback_queue.put_nowait(
                _PlaybackItem(
                    epoch=self._playback_epoch,
                    kind="audio",
                    window_id=window_id,
                    pcm=frame,
                )
            )
        self._playback_partial = bytearray(combined[complete_bytes:])
        self._playback_frames_enqueued += frame_count
        self._playback_buffered_frames += frame_count
        self._playback_queue_peak_frames = max(
            self._playback_queue_peak_frames,
            self._playback_buffered_frames,
        )

    def _flush_playback_partial(self, *, window_id: str | None = None) -> None:
        if not self._playback_partial:
            return
        if (
            self._playback_buffered_frames >= self._playback_frame_capacity
            or self._playback_queue.full()
        ):
            self._dropped_playback_frames += 1
            self._playback_overflow_events += 1
            raise _PlaybackBufferOverflow(
                "gemini_playback_backlog_exceeded while flushing final frame"
            )
        frame = bytes(self._playback_partial).ljust(HFP_FRAME_BYTES, b"\x00")
        self._playback_partial.clear()
        self._playback_queue.put_nowait(
            _PlaybackItem(
                epoch=self._playback_epoch,
                kind="audio",
                window_id=window_id,
                pcm=frame,
            )
        )
        self._playback_frames_enqueued += 1
        self._playback_buffered_frames += 1
        self._playback_queue_peak_frames = max(
            self._playback_queue_peak_frames,
            self._playback_buffered_frames,
        )

    def _clear_playback_queue(self) -> None:
        self._clear_queue(self._playback_queue)
        self._playback_buffered_frames = 0
