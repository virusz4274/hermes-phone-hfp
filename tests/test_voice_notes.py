"""Live voice memory access follows the bound caller's existing permissions."""
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hfp_mcp import gemini_live, server
from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.mark.parametrize('admin,tools,remember', [
    (True, [], True), (False, ['hfp_caller_read', 'hfp_caller_update'], True),
    (False, ['hfp_caller_read'], True), (False, ['hfp_caller_update'], True),
    (False, [], True), (True, [], False),
])
async def test_direct_notes_authorization_and_isolation(tmp_path, monkeypatch, admin, tools, remember):
    monkeypatch.setenv('TEST_BRIDGE_TOKEN', 'b' * 40)
    data = routing_data()
    data['policies']['owner'] = dict(admin=admin, tools=tools, remember=remember)
    config = RoutingConfig.parse(data)
    store = CallerStore(tmp_path / 'callers.sqlite3')
    policy = asdict(config.policies['owner'])
    policy['tools'] = list(policy['tools'])
    store.bind(session_id='sid', call_id='call', profile='default', number='+919876543210',
               policy=policy, ttl=60, remember=remember)
    caller = store.caller_id('+919876543210')
    store.replace_notes('default', caller, 'Dated RAM delivery')
    store.replace_notes('default', store.caller_id('+919876543211'), 'Other caller secret')
    store.replace_notes('other-profile', caller, 'Other profile secret')
    bridge = PhoneBridge(config, store, profile='default', profile_config={})
    app = web.Application()
    bridge.wire(app)
    try:
        async with TestClient(TestServer(app)) as client:
            async def request(**body):
                return await client.post('/v1/hfp/bindings/sid/notes', json=body,
                    headers={'X-HFP-Bridge-Token': 'b' * 40})
            read = await request(action='read')
            assert read.status == (200 if admin or 'hfp_caller_read' in tools else 403)
            if read.status == 200:
                result = await read.json()
                assert result['notes'] == ('Dated RAM delivery' if remember else '')
                assert result['persistent'] is remember
                if not remember:
                    assert 'disabled' in result['message']
            assert (await request(action='read', number='+919876543211')).status == 400
            assert (await request(action='read', profile='other-profile')).status == 400
            update = await request(action='update', notes='Dated RAM delivery; meeting today', expected_revision=1)
            writable = remember and (admin or 'hfp_caller_update' in tools)
            assert update.status == (200 if writable else 403)
            if writable:
                assert (await request(action='update', notes='stale replacement', expected_revision=1)).status == 409
                assert store.read('default', caller) == 'Dated RAM delivery; meeting today'
            else:
                assert store.read('default', caller) == 'Dated RAM delivery'
            store.revoke('sid')
            assert (await request(action='read')).status == 403
            assert store.read('other-profile', caller) == 'Other profile secret'
    finally:
        store.close()


@pytest.mark.parametrize('policy,exposed', [
    ({'admin': True}, True), ({'tools': ['hfp_caller_read']}, True),
    ({'tools': ['hfp_caller_update']}, False), ({'tools': []}, False),
])
async def test_normal_voice_notes_keep_hermes_tools_and_current_memory(monkeypatch, policy, exposed):
    monkeypatch.setattr(server, '_gemini_live_manager', None)
    monkeypatch.setattr(server, '_gemini_allowed_tools', frozenset())
    data = routing_data()
    data['policies']['owner'] = policy
    config = RoutingConfig.parse(data)
    c = server._create_phone_controller(config)
    c.route = config.resolve('+919876543210')[0]
    c.call_id = 'call'
    c.snapshot = lambda: {'call': {'id': 'call', 'state': 'active'}}
    c.binding = {'session_id': 'sid', 'notes': 'Old snapshot', 'persistent': True}
    calls = []
    class API:
        async def binding_notes(self, sid, **args):
            calls.append(args)
            return {'notes': '2026-09-22: delivery and RAM replacement', 'revision': 2, 'persistent': True}
    c.api = API()
    voice = c.make_voice('gemini_live', c)
    allowed = voice.manager._allowed_tools
    assert ('phone_notes' in allowed) is exposed
    assert 'ask_hermes' in allowed
    prompt = gemini_live._live_config(c.voice_context(), None, allowed)['system_instruction']
    assert 'No native Hermes agent or external-action tool is available' not in prompt
    result = await c.delegate(SimpleNamespace(name='phone_notes', arguments={'action': 'read'}))
    assert result['status'] == ('ok' if exposed else 'error')
    assert len(calls) == int(exposed)
    if exposed:
        assert 'delivery and RAM replacement' in await voice.manager.context_provider()
    if not policy.get('admin'):
        result = await c.delegate(SimpleNamespace(name='phone_notes', arguments={'action': 'update', 'notes': 'new', 'expected_revision': 2}))
        assert result['status'] == ('ok' if 'hfp_caller_update' in policy.get('tools', []) else 'error')


@pytest.mark.parametrize('code,expected', [(403, 'denied'), (409, 'Read current notes'), (500, 'failed')])
async def test_notes_lookup_failure_is_not_reported_as_empty(code, expected):
    c = server._create_phone_controller(RoutingConfig.parse(routing_data()))
    c.route = c.config.resolve('+919876543210')[0]
    c.call_id = 'call'
    c.binding = {'session_id': 'sid', 'notes': 'Existing fact'}
    c.snapshot = lambda: {'call': {'id': 'call', 'state': 'active'}}
    class API:
        async def binding_notes(self, *args, **kwargs):
            httpx.Response(code, request=httpx.Request('POST', 'http://hermes/notes')).raise_for_status()
    c.api = API()
    result = await c.delegate(SimpleNamespace(name='phone_notes', arguments={'action': 'read'}))
    assert result['status'] == 'error' and expected in result['message']
    assert c.binding['notes'] == 'Existing fact'


def test_notes_tool_cannot_bypass_continuity_transcription_requirement(monkeypatch):
    monkeypatch.setenv('HFP_GEMINI_INPUT_TRANSCRIPTION', 'false')
    with pytest.raises(ValueError, match='requires Gemini input and output transcription'):
        gemini_live.GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None,
            allowed_tools={'phone_notes', 'phone_recall', 'ask_hermes'}, context_provider=lambda: '')
