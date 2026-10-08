import asyncio
import json
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest

from hfp_mcp import gemini_live, server
from hfp_mcp.audio.opening import Opening, outgoing_opening
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.mark.parametrize('direction', ['incoming', 'outgoing'])
def test_route_attaches_opening_only_to_outgoing(monkeypatch, direction):
    monkeypatch.setattr(server, '_gemini_live_manager', None)
    monkeypatch.setattr(server, '_gemini_allowed_tools', frozenset())
    monkeypatch.delenv('HFP_GEMINI_OUTGOING_GREETING', raising=False)
    c = server._create_phone_controller(RoutingConfig.parse(routing_data()))
    c.route = c.config.resolve('+919876543210')[0]
    c.snapshot = lambda: {'call': {'direction':direction}}
    voice = c.make_voice('gemini_live', c)
    assert bool(voice.manager.outgoing_opening) == (direction == 'outgoing')


def test_cached_formats_are_bounded_and_do_not_need_tts():
    spoken = outgoing_opening()
    assert 1600 < len(spoken.pcm) <= 96000
    assert spoken.text == 'Hello, this is Hermes, an AI assistant.'
    assert outgoing_opening() is spoken
    assert len(outgoing_opening('chime').pcm) == 4800
    assert outgoing_opening('off') is None
    with pytest.raises(ValueError): outgoing_opening('unknown')


def test_cached_introduction_replaces_model_introduction_instruction():
    plain = gemini_live._live_config('', None, set())['system_instruction']
    cached = gemini_live._live_config('', None, set(), cached_spoken_opening=True)['system_instruction']
    assert "after the recipient's hello, introduce yourself" in plain
    assert "after the recipient's hello, introduce yourself" not in cached
    assert 'without reintroducing yourself' in cached


@asynccontextmanager
async def pipeline(monkeypatch, opening=True, fail_send=False, fail_first_input=False):
    async def ok(*_): return {'ok':True}
    clears, configs, provider_text = [], [], []
    async def clear(*args): clears.append(args)
    manager = gemini_live.GeminiLiveManager(ensure_stream=ok, clear_playback=clear,
        hangup=ok, full_transcripts_enabled=True)
    manager._session_id = 'physical-one'
    if opening:
        manager.outgoing_opening = Opening(struct.pack('<h', 1234)*2400, 'Hello, this is Hermes, an AI assistant.')
    manager._state = gemini_live.LiveAIState.STARTING
    gate = asyncio.Event()
    class WS:
        def __init__(self): self.incoming=asyncio.Queue(); self.sent=[]; self.provider_audio=[]
        def __aiter__(self): return self
        async def __anext__(self): return await self.incoming.get()
        async def send_str(self, text): self.sent.append(json.loads(text))
        async def send_bytes(self, pcm):
            if fail_send: raise OSError('synthetic transport failure')
            self.sent.append(pcm)
    ws = WS()
    class Session:
        async def send_realtime_input(self, **kwargs):
            if 'text' in kwargs: provider_text.append(kwargs['text'])
            if 'audio' in kwargs:
                ws.provider_audio.append(kwargs['audio'].data)
                if fail_first_input and len(ws.provider_audio) == 1:
                    raise OSError('uncertain input send')
        async def receive(self):
            await manager._stop_event.wait()
            if False: yield None
    class Connection:
        async def __aenter__(self): await gate.wait(); return Session()
        async def __aexit__(self, *_): pass
    class Live:
        def connect(self, **kwargs): configs.append(kwargs['config']); return Connection()
    class Client:
        def __init__(self, **kwargs): self.aio=SimpleNamespace(live=Live(), aclose=ok)
    class WSContext:
        async def __aenter__(self): return ws
        async def __aexit__(self, *_): pass
    class HTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def ws_connect(self, *args, **kwargs): return WSContext()
    from google import genai
    monkeypatch.setattr(genai, 'Client', Client)
    monkeypatch.setattr(aiohttp, 'ClientSession', HTTP)
    runner = asyncio.create_task(manager._run_provider('synthetic', 'This is an outgoing call.'))
    try:
        yield manager, ws, gate, clears, configs, provider_text, runner
    finally:
        manager.signal_stop()
        await ws.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED))
        await asyncio.gather(runner, return_exceptions=True)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate(): await asyncio.sleep(.005)


async def test_opening_reaches_hfp_before_provider_ready_once(monkeypatch):
    async with pipeline(monkeypatch) as (m, ws, gate, clears, configs, text, runner):
        await until(lambda: m._opening_status['state'] == 'sent_to_hfp')
        assert not gate.is_set()
        assert b''.join(x for x in ws.sent if isinstance(x, bytes)) == m.outgoing_opening.pcm
        assert len([x for x in ws.sent if isinstance(x, dict) and x['type']=='playback_start']) == 1
        assert not m.assistant_hangup_ready()  # our own opening is not caller speech
        assert m.get_call_transcript()['events'][-1]['metadata']['source'] == 'cached_outgoing_opening'
        gate.set()
        await until(lambda: 'provider_ready' in m._timings)
        assert not text  # no model-generated startup turn
        assert 'Do not repeat this introduction' in configs[0]['system_instruction']
        await m.reset_conversation('reset context', 1)
        m._queue_outgoing_opening()
        await until(lambda: len(configs) == 2)
        assert len([x for x in ws.sent if isinstance(x, dict) and x['type']=='playback_start']) == 1


async def test_recipient_interrupts_local_opening_without_provider(monkeypatch):
    async with pipeline(monkeypatch) as (m, ws, gate, clears, configs, text, runner):
        await until(lambda: m._opening_status['state'] == 'playing')
        # Quiet microphone and a single click do not interrupt.
        for pcm in [bytes(320), struct.pack('<h',3000)*160, bytes(320)]:
            await ws.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=pcm))
        await asyncio.sleep(.03)
        assert m._opening_pending()
        for _ in range(4):
            await ws.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=struct.pack('<h',3000)*160))
        await until(lambda: m._opening_status['state'] == 'interrupted')
        assert clears and not gate.is_set()
        sent = sum(len(x) for x in ws.sent if isinstance(x, bytes))
        await asyncio.sleep(.05)
        assert sum(len(x) for x in ws.sent if isinstance(x, bytes)) == sent
        assert sent < len(m.outgoing_opening.pcm)
        m._queue_outgoing_opening()
        assert m._opening_status['state'] == 'interrupted'


async def test_incoming_pipeline_has_no_local_opening(monkeypatch):
    async with pipeline(monkeypatch, opening=False) as (m, ws, gate, *_):
        gate.set()
        await until(lambda: 'provider_ready' in m._timings)
        assert not ws.sent
        assert m._opening_status['state'] == 'disabled'


async def test_model_reply_clears_local_playback_before_enqueue(monkeypatch):
    async with pipeline(monkeypatch) as (m, ws, gate, clears, configs, text, runner):
        await until(lambda: m._opening_status['state'] == 'playing')
        class Reply:
            async def receive(self):
                yield SimpleNamespace(server_content=SimpleNamespace(
                    input_transcription=SimpleNamespace(text='Hello.', finished=True),
                    output_transcription=None, model_turn=SimpleNamespace(parts=[SimpleNamespace(
                        inline_data=SimpleNamespace(mime_type='audio/pcm;rate=24000',
                            data=struct.pack('<h',2300)*960))]),
                    generation_complete=True, turn_complete=True), tool_call=None)
                await asyncio.Future()
        receiver = asyncio.create_task(m._gemini_receiver(Reply()))
        try:
            await until(lambda: m._opening_status['state'] == 'interrupted' and m._generation_count == 1)
            assert clears
            # Only the new reply can remain queued after clearing the clip.
            assert m._playback_buffered_frames <= 2
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


async def test_uncertain_send_is_not_retried_on_same_call(monkeypatch):
    async with pipeline(monkeypatch, fail_send=True) as (m, ws, gate, clears, configs, text, runner):
        with pytest.raises(OSError): await runner
        assert m._opening_status['state'] == 'cancelled'
        m._reset_media_queues()
        m._queue_outgoing_opening()
        assert m._playback_queue.empty()
        m._session_id = 'physical-two'
        m._queue_outgoing_opening()
        assert m._opening_status['state'] == 'queued'
