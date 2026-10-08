import asyncio
from types import SimpleNamespace

import pytest

from hfp_mcp import gemini_live, server
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.mark.parametrize('direction', ['incoming', 'outgoing'])
@pytest.mark.parametrize('transition', ['reconnect', 'reset'])
async def test_call_setup_does_not_trigger_greeting(direction, transition):
    async def ok(*_): return {'ok': True}
    manager = gemini_live.GeminiLiveManager(ensure_stream=ok, clear_playback=ok, hangup=ok)
    manager._caller_spoke = True
    sent, configs = [], []
    class Session:
        async def send_realtime_input(self, **kwargs):
            assert manager._provider_send_lock.locked()
            sent.append(kwargs)
        async def receive(self):
            if len(configs) == 1:
                if transition == 'reset':
                    await manager.reset_conversation('new conversation', 1)
                    await asyncio.Future()
                else:
                    yield SimpleNamespace(server_content=None, tool_call=None, tool_call_cancellation=None,
                        session_resumption_update=SimpleNamespace(resumable=True, new_handle='resume'), go_away=SimpleNamespace(time_left='1s'))
            else:
                manager._stop_event.set()
            if False: yield None
    class Connection:
        async def __aenter__(self): return Session()
        async def __aexit__(self, *_): pass
    class Live:
        def connect(self, **kwargs):
            configs.append(kwargs['config']); return Connection()
    async def sender(_): await asyncio.Future()
    manager._gemini_input_sender = sender
    await asyncio.wait_for(manager._session_supervisor(SimpleNamespace(aio=SimpleNamespace(live=Live())),
        'This is an ' + direction + ' call.'), 3)
    assert len(configs) == 2
    assert manager._caller_spoke  # physical-call evidence survives reconnect/reset
    assert not sent
    assert all('wait for the caller or recipient to speak first' in config['system_instruction']
               for config in configs)
    assert all('after the recipient\'s hello, introduce yourself' in config['system_instruction']
               for config in configs)


@pytest.mark.parametrize('admin,expected', [(True, True), (False, False)])
def test_owner_tool_visibility_is_admin_only(monkeypatch, admin, expected):
    monkeypatch.setattr(server, '_gemini_live_manager', None)
    monkeypatch.setattr(server, '_gemini_allowed_tools', frozenset())
    data = routing_data(); data['policies']['owner'] = {'admin': admin}
    c = server._create_phone_controller(RoutingConfig.parse(data))
    c.route = c.config.resolve('+919876543210')[0]
    c.snapshot = lambda: {'call': {'direction': 'incoming'}}
    voice = c.make_voice('gemini_live', c)
    assert ('owner_call_lookup' in voice.manager._allowed_tools) is expected


async def test_historical_hangup_cannot_end_new_call(monkeypatch):
    monkeypatch.setenv('HFP_GEMINI_INPUT_TRANSCRIPTION', 'true')
    async def ok(*_): return {'ok': True}
    manager = gemini_live.GeminiLiveManager(ensure_stream=ok, clear_playback=ok,
        hangup=ok, allowed_tools={'end_call'})
    replies = []
    async def respond(call_id, name, payload): replies.append((call_id, payload))
    manager._send_function_response = respond
    def request(call_id):
        return SimpleNamespace(tool_call=SimpleNamespace(function_calls=[
            SimpleNamespace(id=call_id, name='end_call', args={'cancel_wait': True})]))

    # Prior text/output/empty input cannot authorize a hangup in this physical call.
    manager._last_input_transcript = 'Please disconnect the call.'
    manager._handle_transcriptions(SimpleNamespace(input_transcription=SimpleNamespace(text=' '),
        output_transcription=SimpleNamespace(text='Goodbye.')))
    await manager._handle_tool_calls(request('historical'))
    await manager._handle_tool_calls(request('historical'))
    assert manager._requests.empty()
    assert manager._broker_tool_calls_accepted == 0
    assert len(replies) == 2
    assert 'No caller speech' in str(replies[0][1])

    # Even an interim utterance authorizes the ordinary current-call path.
    manager._handle_transcriptions(SimpleNamespace(input_transcription=SimpleNamespace(
        text='Please disconnect this call.', finished=False), output_transcription=None))
    await manager._handle_tool_calls(request('current'))
    assert manager._requests.qsize() == 1
    manager._cancel_all_tool_tasks()
    manager._reset_call_metrics()
    assert not manager.assistant_hangup_ready()


def test_transcription_disabled_startup_fence_is_bounded(monkeypatch):
    monkeypatch.setenv('HFP_GEMINI_INPUT_TRANSCRIPTION', 'false')
    async def ok(*_): return {'ok': True}
    manager = gemini_live.GeminiLiveManager(ensure_stream=ok, clear_playback=ok, hangup=ok)
    assert not manager.assistant_hangup_ready()
    manager._startup_probe.sources['captured'] = {'above_threshold_ms': 100}
    assert not manager.assistant_hangup_ready()  # audio must reach the provider
    manager._startup_probe.sources['submitted'] = {'above_threshold_ms': 60}
    assert manager.assistant_hangup_ready()
    manager._reset_call_metrics()
    manager._metrics_started -= 31
    assert manager.assistant_hangup_ready()
