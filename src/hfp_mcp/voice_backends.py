"""Voice backends share the controller's caller binding and Hermes run path."""

from __future__ import annotations

import asyncio
import math
import re
import struct
import time
import uuid
from contextlib import aclosing


class GeminiVoice:
    name = "gemini_live"

    def __init__(self, manager):
        self.manager = manager

    async def start(self, call_id, context):
        result = await self.manager.start(call_id, context)
        if not result.get("ok"):
            raise RuntimeError("Gemini startup failed")

    def healthy(self):
        return self.manager.running

    async def stop(self):
        await self.manager.stop()

    async def notify(self, result, request_id):
        if request_id in self.manager._pending:
            raise RuntimeError("initial function response has not been sent")
        await self.manager.send_task_update(result)

    async def refresh(self, context, generation):
        await self.manager.reset_conversation(context, generation)


class ClassicVoice:
    name = "classic"

    def __init__(self, controller, *, acquire, release, clear):
        self.controller = controller
        self.acquire, self.release, self.clear = acquire, release, clear
        self.stream = None
        self.ws = self.http = None
        self.tasks = []
        self.turn_task = None
        self.utterances = asyncio.Queue(maxsize=8)
        self.sentences = asyncio.Queue(maxsize=4)
        self.epoch = 0
        self.tail_audio = b""
        self.transcribing_pcm = None
        self.speech_times = {}
        self.transcribing_at = None

    async def start(self, call_id, context):
        import aiohttp

        self.stream = await self.acquire(call_id)
        if not self.stream.get("ok"):
            raise RuntimeError("classic audio lease unavailable")
        result = self.stream.get("result", self.stream)
        self.stream_id = result["stream_id"]
        self.http = aiohttp.ClientSession()
        self.ws = await self.http.ws_connect(
            result.get("client_stream_url") or result["stream_url"]
        )
        self.tasks = [
            asyncio.create_task(fn()) for fn in (self.capture, self.turns, self.speak)
        ]

    def healthy(self):
        return bool(self.tasks) and all(not task.done() for task in self.tasks)

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Audio already received before hangup can still yield the last caller words.
        pending = []
        if self.transcribing_pcm:
            pending.append((self.transcribing_pcm, self.transcribing_at))
            self.transcribing_pcm = None
        while not self.utterances.empty():
            pcm, epoch = self.utterances.get_nowait()
            pending.append((pcm, self.speech_times.pop(epoch, None)))
        if len(self.tail_audio) >= 4800:
            pending.append((self.tail_audio, self.speech_times.get(self.epoch)))
        self.tail_audio = b""
        if pending and getattr(self.controller, "memory_capture", None):
            try:
                async with asyncio.timeout(25):
                    for pcm, stamp in pending:
                        result = await self.controller.api.request("POST", "v1/hfp/speech/transcribe", bridge=True,
                            content=pcm, headers={"Content-Type": "application/octet-stream"})
                        if result.get("text", "").strip():
                            self.controller.record_transcript("input", result["text"].strip(), timestamp=stamp)
            except Exception:
                self.controller.memory_capture.data['capture_complete'] = False
        if self.ws:
            await self.ws.close()
        if self.http:
            await self.http.close()
        if self.stream:
            result = self.stream.get("result", self.stream)
            if result.get("stream_id"):
                await self.release(result["stream_id"])
        self.tasks = []

    async def capture(self):
        import aiohttp

        speech = bytearray()
        pre_roll = bytearray()
        voiced_at = 0.0
        async for message in self.ws:
            if message.type != aiohttp.WSMsgType.BINARY:
                continue
            frame = bytes(message.data)
            if len(frame) % 2:
                raise ValueError("invalid PCM frame")
            samples = struct.unpack("<" + "h" * (len(frame) // 2), frame)
            level = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
            now = time.monotonic()
            if level >= 200:
                if not speech:
                    self.epoch += 1
                    self.speech_times[self.epoch] = time.time()
                    if self.turn_task:
                        self.turn_task.cancel()
                    await self.clear(self.stream_id)
                    speech.extend(pre_roll)
                    pre_roll.clear()
                speech.extend(frame)
                voiced_at = now
            elif speech:
                speech.extend(frame)
            else:
                pre_roll.extend(frame)
                del pre_roll[:-3200]
            if speech and (now - voiced_at >= 0.7 or len(speech) >= 480000):
                if len(speech) >= 4800:
                    # A full queue fails visibly instead of silently losing requests.
                    self.utterances.put_nowait((bytes(speech), self.epoch))
                speech.clear()
            self.tail_audio = bytes(speech)

    async def turns(self):
        while True:
            pcm, epoch = await self.utterances.get()
            if epoch != self.epoch:
                continue
            self.turn_task = asyncio.create_task(self.handle_turn(pcm, epoch))
            try:
                await self.turn_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.turn_task = None

    async def handle_turn(self, pcm, epoch):
        api = self.controller.api
        self.transcribing_pcm = pcm
        self.transcribing_at = self.speech_times.pop(epoch, time.time())
        result = await api.request(
            "POST",
            "v1/hfp/speech/transcribe",
            bridge=True,
            content=pcm,
            headers={"Content-Type": "application/octet-stream"},
        )
        text = result.get("text", "").strip()
        if text:
            self.controller.record_transcript("input", text, timestamp=self.transcribing_at)
        self.transcribing_pcm = None
        if not text or epoch != self.epoch:
            return
        buffer, emitted = "", False
        async with aclosing(self.controller.events(text, uuid.uuid4().hex)) as events:
            async for event in events:
                if event.get("type") == "message.delta":
                    buffer += event.get("delta", "")
                    while True:
                        match = re.search(r"[.!?]\s|\n", buffer)
                        if not match and len(buffer) < 1500:
                            break
                        split = match.end() if match else 1500
                        await self.sentences.put((buffer[:split], epoch))
                        buffer = buffer[split:]
                        emitted = True
                elif event.get("type") == "approval.request":
                    await self.sentences.put(
                        ("That action is waiting for owner approval.", epoch)
                    )
                elif event.get("type") == "result" and not buffer and not emitted:
                    buffer = str(event.get("output") or "")
        if buffer:
            await self.sentences.put((buffer, epoch))

    async def speak(self):
        while True:
            text, epoch = await self.sentences.get()
            if epoch != self.epoch:
                continue
            api = self.controller.api
            response = await api.http.post(
                "v1/hfp/speech/synthesize",
                json={"text": text[:2000]},
                headers={"X-HFP-Bridge-Token": api.endpoint.secret(True)},
            )
            response.raise_for_status()
            pcm = response.content
            self.controller.record_transcript("output", text[:2000], metadata={"delivery": "generated"})
            for offset in range(0, len(pcm), 320):
                if epoch != self.epoch:
                    break
                await self.ws.send_bytes(pcm[offset : offset + 320].ljust(320, b"\0"))
                await asyncio.sleep(0.02)
