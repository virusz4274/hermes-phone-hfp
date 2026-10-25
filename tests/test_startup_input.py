"""Preserve an early hello while setup is delayed, without replay on rotation."""
import asyncio
import struct
from types import SimpleNamespace

import aiohttp
import pytest

from hfp_mcp import gemini_live
from tests.test_outgoing_opening import pipeline, until


class IdentityResampler:
    def __init__(self, *_): pass
    def convert(self, pcm, **_): return pcm


async def feed(ws, frames):
    for frame in frames:
        await ws.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=frame))


@pytest.mark.parametrize('opening', [False, True])
async def test_early_hello_then_silence_survives_delayed_setup_once(monkeypatch, opening):
    monkeypatch.setattr(gemini_live, 'PcmResampler', IdentityResampler)
    async with pipeline(monkeypatch, opening=opening) as (m, ws, gate, *_):
        await until(lambda: m._ws is not None)
        # Two seconds of captured audio: hello first, then quiet until setup.
        hello = [struct.pack('<h', 1200 + i) * 160 for i in range(10)]
        frames = hello + [bytes(320)] * 90
        await feed(ws, frames)
        await until(lambda: m._input_frames_received == 100)
        assert not gate.is_set() and not ws.provider_audio
        assert len(m._startup_input) == 100 and m._input_queue.empty()
        gate.set()
        await until(lambda: m._input_frames_submitted == 100)
        assert b''.join(ws.provider_audio) == b''.join(frames)
        assert all(len(chunk) <= 640 for chunk in ws.provider_audio)
        assert not m._input_queue_overflow_frames
        assert not m._input_startup_trimmed_frames
        assert m.status()['startup_input_buffer']['released_to_sender_frames'] == 100
        # Subsequent live speech is fresh and appended once, not a second hello.
        later = struct.pack('<h', 2400) * 160
        await feed(ws, [later])
        await until(lambda: m._input_frames_submitted == 101)
        assert b''.join(ws.provider_audio) == b''.join(frames) + later
        await m.reset_conversation('new context', 1)
        await until(lambda: len(ws.provider_audio) == 51 and m._session_generation == 2)
        await asyncio.sleep(.04)
        assert len(ws.provider_audio) == 51 and not m._startup_input_pending


async def test_uncertain_startup_send_does_not_replay_after_reconnect(monkeypatch):
    monkeypatch.setattr(gemini_live, 'PcmResampler', IdentityResampler)
    async with pipeline(monkeypatch, opening=False, fail_first_input=True) as (m, ws, gate, _, configs, *_):
        await until(lambda: m._ws is not None)
        frames = [struct.pack('<h', 1200 + i) * 160 for i in range(100)]
        await feed(ws, frames)
        await until(lambda: m._input_frames_received == 100)
        gate.set()
        await until(lambda: len(configs) == 2 and m._session_generation == 2)
        assert ws.provider_audio == [b''.join(frames[:2])]
        assert m._input_frames_submitted == 0 and m._input_send_failure_frames == 2
        assert m._startup_input_discarded == 98 and not m._startup_input_pending
        await feed(ws, [bytes(320)])
        await until(lambda: m._input_frames_submitted == 1)
        assert ws.provider_audio == [b''.join(frames[:2]), bytes(320)]


async def test_startup_buffer_is_bounded_and_normal_queue_stays_fresh(monkeypatch):
    async with pipeline(monkeypatch, opening=False) as (m, ws, gate, *_):
        await until(lambda: m._ws is not None)
        frames = [struct.pack('<h', i) * 160 for i in range(300)]
        await feed(ws, frames)
        await until(lambda: m._input_frames_received == 300)
        assert list(m._startup_input) == frames[-250:]
        assert m._startup_input_overflow == 50 and m._dropped_input_frames == 50
        m._discard_startup_input()
        for frame in frames[:15]: m._queue_input_frame(frame)
        assert m._input_queue.qsize() == 10 and m._input_queue_overflow_frames == 5
        assert m.status()['startup_input_buffer']['discarded_frames'] == 250


async def test_pending_setup_audio_cleared_at_call_end_and_new_call(monkeypatch):
    async with pipeline(monkeypatch, opening=False) as (m, ws, gate, *_):
        await until(lambda: m._ws is not None)
        await feed(ws, [struct.pack('<h', 2000) * 160] * 8)
        await until(lambda: m._input_frames_received == 8)
    assert not m._startup_input and not m._startup_input_pending
    assert m._startup_input_discarded == 8
    m._reset_media_queues()
    m._reset_call_metrics()
    assert not m._startup_input and m._startup_input_captured == 0
    assert m._startup_input_released == 0 and m._startup_input_discarded == 0


async def test_reset_discards_unsent_startup_audio_before_new_session(monkeypatch):
    monkeypatch.setattr(gemini_live, 'PcmResampler', IdentityResampler)
    async with pipeline(monkeypatch, opening=False) as (m, ws, gate, _, configs, *_):
        await until(lambda: m._ws is not None)
        await feed(ws, [struct.pack('<h', 1800) * 160] * 100)
        await until(lambda: m._input_frames_received == 100)
        await m._provider_send_lock.acquire()
        gate.set()
        try:
            await until(lambda: m._startup_input_released == 2)
            await m.reset_conversation('fresh session', 1)
            assert not m._startup_input and m._startup_input_discarded == 98
            await until(lambda: len(configs) == 2 and m._session_generation == 2)
        finally:
            m._provider_send_lock.release()
        await asyncio.sleep(.04)
        assert not ws.provider_audio
        await feed(ws, [bytes(320)])
        await until(lambda: m._input_frames_submitted == 1)
        assert ws.provider_audio == [bytes(320)]
