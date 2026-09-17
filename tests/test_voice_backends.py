import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from hfp_mcp.voice_backends import ClassicVoice
from tests.test_phone_controller import wait_for


async def test_classic_barge_in_closes_old_run_and_accepts_next_turn():
    entered, revoked = asyncio.Event(), asyncio.Event()

    async def events(text, request_id):
        if text == "first":
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {}
            finally:
                revoked.set()
        else:
            yield {"type": "result", "output": "Second reply."}

    api = SimpleNamespace(
        request=AsyncMock(side_effect=[{"text": "first"}, {"text": "second"}])
    )
    clear = AsyncMock()
    voice = ClassicVoice(
        SimpleNamespace(api=api, events=events, record_transcript=Mock()),
        acquire=None,
        release=None,
        clear=clear,
    )
    voice.stream_id = "stream"
    voice.epoch = 1
    voice.utterances.put_nowait((b"first", 1))
    worker = asyncio.create_task(voice.turns())
    try:
        await asyncio.wait_for(entered.wait(), 1)

        async def incoming():
            yield SimpleNamespace(
                type=aiohttp.WSMsgType.BINARY,
                data=struct.pack("<160h", *([1000] * 160)),
            )

        voice.ws = incoming()
        await voice.capture()
        await asyncio.wait_for(revoked.wait(), 1)
        clear.assert_awaited_once_with("stream")
        voice.utterances.put_nowait((b"stale", 1))
        voice.utterances.put_nowait((b"second", 2))
        assert await asyncio.wait_for(voice.sentences.get(), 1) == ("Second reply.", 2)
        assert api.request.await_count == 2
        assert not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_classic_cancel_while_sentence_queue_full_closes_run():
    closed = asyncio.Event()

    async def events(*args):
        try:
            yield {"type": "message.delta", "delta": "A sentence. "}
        finally:
            closed.set()

    voice = ClassicVoice(
        SimpleNamespace(
            api=SimpleNamespace(request=AsyncMock(return_value={"text": "hello"})),
            events=events,
            record_transcript=Mock(),
        ),
        acquire=None,
        release=None,
        clear=None,
    )
    for _ in range(4):
        voice.sentences.put_nowait(("queued", 0))
    task = asyncio.create_task(voice.handle_turn(b"pcm", 0))
    await wait_for(lambda: voice.controller.api.request.await_count == 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()
