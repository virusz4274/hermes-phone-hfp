import copy
import json
import time
from dataclasses import asdict

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hfp_mcp import hermes_api
from hfp_mcp.caller_context import CallerStore
from hfp_mcp.contracts import RequestLedger
from hfp_mcp.conversations import ConversationStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.owner_lookup import lookup, page_records, validate_query
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data

OWNER = '+919876543210'
FRIEND = '+919876543211'
NOTES_ONLY_CALLER = '+919876543212'


@pytest.fixture
async def system(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_BRIDGE_TOKEN', 'b' * 40)
    data = routing_data()
    data['endpoints']['friends'] = {**data['endpoints']['owner'], 'profile': 'friends', 'url': 'http://127.0.0.1:8643'}
    data['numbers'][FRIEND] = {'endpoint': 'friends', 'policy': 'guest'}
    data['numbers'][NOTES_ONLY_CALLER] = {'endpoint': 'owner', 'policy': 'guest'}
    data['owner_access'] = {'owner': ['friends']}
    config = RoutingConfig.parse(data)
    stores, bridges, clients = {}, {}, {}
    for name, endpoint in config.endpoints.items():
        store = CallerStore(tmp_path / name / 'callers.sqlite3')
        bridge = PhoneBridge(config, store, profile=endpoint.profile, profile_config={})
        app = web.Application(); bridge.wire(app)
        client = TestClient(TestServer(app)); await client.start_server()
        stores[name], bridges[name], clients[name] = store, bridge, client
    stores['owner'].bind(session_id='hfp-admin', call_id='owner-call', profile='default', number=OWNER, policy={'admin': True}, ttl=60)
    stores['owner'].bind(session_id='hfp-guest', call_id='guest-call', profile='default', number=OWNER, policy={'admin': False}, ttl=60)
    stores['owner'].replace_notes('default', stores['owner'].caller_id(OWNER), 'Owner private note')
    stores['owner'].replace_notes('default', stores['owner'].caller_id(NOTES_ONLY_CALLER),
                                  'Manually entered: caller wants to discuss the console incident')
    stores['friends'].replace_notes('friends', stores['friends'].caller_id(FRIEND), 'Friend reported RAM delivery')
    ledger = RequestLedger(tmp_path / 'calls.db')
    conversation = ConversationStore(ledger).open('friends', stores['friends'].caller_id(FRIEND))
    ledger.append_transcript({'session_id': 'friend-call', 'provider': 'gemini_live', 'direction': 'input',
                             'text': 'Delivery arrives Friday', 'timestamp': time.time(),
                             'conversation_id': conversation['id'], 'event_id': 'event'})
    class API:
        revoke_after_read = False
        def __init__(self, endpoint):
            self.name = next(k for k, v in config.endpoints.items() if v == endpoint)
        async def request(self, method, path, **kwargs):
            response = await clients[self.name].post('/' + path, json=kwargs['json'], headers={'X-HFP-Bridge-Token': 'b' * 40})
            if response.status == 403: raise PermissionError('denied')
            assert response.status == 200, await response.text()
            result = await response.json()
            if path.endswith('/notes') and API.revoke_after_read:
                stores['owner'].revoke('hfp-admin')
            return result
        async def close(self): pass
    monkeypatch.setattr(hermes_api, 'HermesAPI', API)
    monkeypatch.setattr('hfp_mcp.owner_lookup.HermesAPI', API)
    yield config, stores, bridges, ledger, API
    ledger.close()
    for client in clients.values(): await client.close()
    for store in stores.values(): store.close()


async def test_admin_cross_scope_lookup_and_note_provenance(system):
    config, stores, bridges, ledger, API = system
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, {'number': FRIEND, 'endpoint': 'friends'}, transcripts_enabled=True)
    record = result['records'][0]
    assert record['notes'] == 'Friend reported RAM delivery'
    assert record['number'] == FRIEND and record['profile'] == 'friends' and record['timestamp'] > 0
    assert stores['friends'].read('friends', stores['friends'].caller_id(FRIEND)) == record['notes']
    records = ledger._db.execute("SELECT detail_json FROM call_audit WHERE action='owner_lookup'").fetchall()
    assert records and 'RAM' not in records[0][0] and FRIEND not in records[0][0]


async def test_admin_browses_seeded_notes_without_number_or_call_history(system):
    config, stores, bridges, ledger, API = system
    authority = {'binding_id': 'hfp-admin'}
    args = {'record_kind': 'notes', 'limit': 1}
    first = await lookup(config, ledger, 'owner', authority, args, transcripts_enabled=True)
    assert first['status'] == 'ok' and first['has_more']
    assert first['records'][0]['number'] == OWNER
    second = await lookup(config, ledger, 'owner', authority,
                          {**args, 'cursor': first['cursor']}, transcripts_enabled=True)
    assert second['status'] == 'ok' and not second['has_more']
    record = second['records'][0]
    assert record['number'] == NOTES_ONLY_CALLER and record['endpoint'] == 'owner'
    assert 'console incident' in record['notes'] and 'call_id' not in record
    targeted = await lookup(config, ledger, 'owner', authority,
                            {'number': NOTES_ONLY_CALLER}, transcripts_enabled=True)
    assert targeted['records'][0]['notes'] == record['notes']
    for kind in ('calls', 'transcripts'):
        history = await lookup(config, ledger, 'owner', authority,
                               {'number': NOTES_ONLY_CALLER, 'record_kind': kind}, transcripts_enabled=True)
        assert history['status'] == 'ok' and not history['records']


async def test_default_scope_and_guest_denial_both_handlers(system):
    config, stores, bridges, ledger, API = system
    for authority in [{'binding_id': 'hfp-guest'}, {'binding_id': 'hfp-missing'}]:
        with pytest.raises(PermissionError):
            await lookup(config, ledger, 'owner', authority, {'number': OWNER}, transcripts_enabled=True)
    config = RoutingConfig.parse({**routing_data(), 'numbers': {OWNER: {'endpoint': 'owner', 'policy': 'owner'}}})
    with pytest.raises(PermissionError):
        await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, {'number': FRIEND, 'endpoint': 'friends'}, transcripts_enabled=True)
    with pytest.raises(PermissionError):
        bridges['friends'].owner_authority({'source_endpoint': 'owner', 'binding_id': 'hfp-admin'})
    stores['owner'].db.execute('UPDATE bindings SET expires=0 WHERE session_id=?', ('hfp-admin',)); stores['owner'].db.commit()
    with pytest.raises(PermissionError):
        await lookup(system[0], ledger, 'owner', {'binding_id': 'hfp-admin'}, {'number': OWNER}, transcripts_enabled=True)


async def test_delayed_results_are_discarded_after_revoke(system):
    config, stores, bridges, ledger, API = system
    API.revoke_after_read = True
    with pytest.raises(PermissionError):
        await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, {'number': FRIEND, 'endpoint': 'friends'}, transcripts_enabled=True)


async def test_transcripts_calls_filters_retention_and_owner_chat(system):
    config, stores, bridges, ledger, API = system
    args = {'number': FRIEND, 'endpoint': 'friends', 'record_kind': 'transcripts', 'call_id': 'friend-call'}
    result = await lookup(config, ledger, 'owner', {'owner_chat': True}, args, transcripts_enabled=True)
    assert result['records'][0]['text'] == 'Delivery arrives Friday'
    assert result['records'][0]['profile'] == 'friends'
    result = await lookup(config, ledger, 'owner', {'owner_chat': True}, args, transcripts_enabled=False)
    assert result['status'] == 'unavailable' and not result['records']
    args['record_kind'] = 'calls'
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, args, transcripts_enabled=True)
    assert result['records'][0]['events'] == 1
    args['start'] = '2100-01-01T00:00:00+00:00'
    assert not (await lookup(config, ledger, 'owner', {'owner_chat': True}, args, transcripts_enabled=True))['records']


async def test_owner_lookup_cannot_expand_authority_by_forged_run(system):
    config, stores, bridges, ledger, API = system
    with pytest.raises(PermissionError):
        await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin', 'run_id': 'forged'}, {'number': OWNER}, transcripts_enabled=True)
    with pytest.raises(PermissionError):
        bridges['owner'].owner_authority({'source_endpoint': 'owner', 'owner_chat': True, 'binding_id': 'hfp-guest'})


@pytest.mark.parametrize('kind', ['notes', 'transcripts'])
def test_pagination_preserves_every_multibyte_character_and_query_binding(kind):
    text = 'മലയാളം 😀 test ' * 5000
    key = 'notes' if kind == 'notes' else 'text'
    records = [{key: text, 'record_kind': kind, 'timestamp': 1, 'profile': 'friends', 'call_id': 'call'}]
    query = validate_query({'record_kind': kind}, 'IN')
    pieces = []
    for _ in range(100):
        result = page_records(records, query, 'scope')
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 12000
        pieces.append(result['records'][0][key])
        if not result['has_more']: break
        query['cursor'] = result['cursor']
    assert ''.join(pieces) == text
    query['limit'] = 21
    with pytest.raises(ValueError): page_records(records, query, 'scope')


@pytest.mark.parametrize('args', [{'limit': 101}, {'limit': True}, {'record_kind': 'messages'}, {'start': '2026-10-07T10:00:00'}, {'profile': 'other'}])
def test_invalid_queries_rejected(args):
    with pytest.raises((TypeError, ValueError)): validate_query(args, 'IN')


def test_owner_scope_config_validates_references():
    for value in [{'owner': ['missing']}, {'missing': []}, {'owner': 'owner'}, []]:
        with pytest.raises(ValueError): RoutingConfig.parse({**routing_data(), 'owner_access': value})


def test_changed_note_cannot_be_spliced_into_old_cursor():
    query = validate_query({}, 'IN')
    original = [{'notes': '😀' * 8000, 'revision': 1}]
    page = page_records(original, query, 'scope')
    assert page['has_more']
    query['cursor'] = page['cursor']
    with pytest.raises(ValueError, match='record changed'):
        page_records([{'notes': '😃' * 8000, 'revision': 2}], query, 'scope')


async def test_direct_voice_result_cannot_enter_replacement_call(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from hfp_mcp import server
    config = RoutingConfig.parse(routing_data())
    c = server._create_phone_controller(config)
    c.route = config.resolve(OWNER)[0]
    c.call_id = 'old-call'; c.binding = {'session_id': 'hfp-admin'}; c.api = object()
    c.snapshot = lambda: {'call': {'id': c.call_id, 'state': 'active'}}
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_lookup(*args, **kwargs):
        entered.set(); await release.wait()
        return {'status': 'ok', 'records': [{'notes': 'private owner data'}]}
    monkeypatch.setattr('hfp_mcp.owner_lookup.lookup', slow_lookup)
    pending = asyncio.create_task(c.delegate(SimpleNamespace(name='owner_call_lookup', arguments={'number': OWNER})))
    await entered.wait()
    c.call_id = 'guest-call'; c.binding = {'session_id': 'hfp-guest'}
    release.set()
    result = await pending
    assert result['status'] == 'denied' and 'records' not in result


async def test_new_calls_without_continuity_or_spoken_output_keep_profile_provenance(system):
    config, stores, bridges, ledger, API = system
    caller_id = stores['friends'].caller_id(FRIEND)
    with ledger._db:
        ledger._db.execute('INSERT INTO phone_call_sources VALUES (?,?,?,?,?,?,?)',
            ('new-call', 'friends', caller_id, 'friends', FRIEND, 1, time.time()))
    args = {'number': FRIEND, 'endpoint': 'friends', 'record_kind': 'calls', 'call_id': 'new-call'}
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, args, transcripts_enabled=True)
    assert result['records'][0]['events'] == 0 and result['records'][0]['number'] == FRIEND
    ledger.append_transcript({'session_id': 'new-call', 'provider': 'gemini_live', 'direction': 'input',
                             'text': 'Unlinked new dialogue', 'timestamp': time.time()})
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, {**args, 'record_kind': 'transcripts'}, transcripts_enabled=True)
    assert result['records'][0]['text'] == 'Unlinked new dialogue'
    # An unlinked legacy call with no authenticated provenance cannot be assigned a profile.
    ledger.append_transcript({'session_id': 'legacy', 'provider': 'gemini_live', 'direction': 'input',
                             'text': 'Unknown provenance', 'timestamp': time.time()})
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-admin'}, {**args, 'record_kind': 'transcripts', 'call_id': 'legacy'}, transcripts_enabled=True)
    assert not result['records']


async def test_delegated_owner_run_uses_its_binding_and_guest_cannot_use_it(system):
    from types import SimpleNamespace
    config, stores, bridges, ledger, API = system
    store = stores['owner']
    root = store.manage('a' * 32, 'hfp-admin')
    store.bind(session_id='hfp-child', call_id='owner-call', profile='default', number=OWNER, policy={'admin': True}, ttl=300)
    store.tasks.save(dict(task_id='task', request_id='request', conversation_id='a' * 32,
        session_id=root, parent_binding='hfp-admin', binding_id='hfp-child', run_id='run',
        status='running', continue_after_call=True, expires_at=time.time() + 300))
    store.bind_run('run', 'hfp-child', root)
    result = await lookup(config, ledger, 'owner', {'binding_id': 'hfp-child', 'run_id': 'run'}, {'number': OWNER}, transcripts_enabled=True)
    assert result['records'][0]['notes'] == 'Owner private note'
    with pytest.raises(PermissionError):
        await lookup(config, ledger, 'owner', {'binding_id': 'hfp-guest', 'run_id': 'run'}, {'number': OWNER}, transcripts_enabled=True)
    store.revoke('hfp-child')
    with pytest.raises(PermissionError):
        await lookup(config, ledger, 'owner', {'binding_id': 'hfp-child', 'run_id': 'run'}, {'number': OWNER}, transcripts_enabled=True)
