import asyncio
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hfp_mcp.call_memory import CallMemory
from hfp_mcp.caller_context import CallerStore, NoteConflict
from hfp_mcp.contracts import RequestLedger
from hfp_mcp.gemini_live import GeminiLiveManager
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.memory_capture import new_capture, recover
from hfp_mcp.phone_controller import PhoneController
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data

NUMBER = '+919876543210'


async def extract(messages):
    body = json.loads(messages[-1]['content'])
    if 'events' not in body:
        return {'keep': [u['id'] for u in body['updates']]}
    return {'updates': [dict(text=e['text'], source_event_id=e['event_id'], kind='fact', due_date=None)
                        for e in body['events'] if e['direction'] == 'input' and e['event_id'] in body['new_event_ids']]}


@pytest.fixture
def system(tmp_path):
    config = RoutingConfig.parse({**routing_data(), 'timezone': 'Asia/Kolkata'})
    store = CallerStore(tmp_path / 'callers.sqlite3')
    policy = asdict(config.policies['owner'])
    policy['tools'] = sorted(policy['tools'])
    store.bind(session_id='binding', call_id='call', profile='default', number=NUMBER, policy=policy, ttl=100)
    bridge = PhoneBridge(config, store, profile='default', profile_config={})
    memory = CallMemory(bridge, generate=extract)
    receipt = memory.begin('binding', number=NUMBER, outbound=False, timezone=config.timezone)
    yield store, bridge, memory, receipt
    store.close()


def event(text, eid='e1', direction='input', timestamp=None):
    return dict(event_id=eid, text=text, direction=direction, timestamp=timestamp or time.time())


async def test_hangup_dated_delivery_and_ram_are_saved_once(system):
    store, bridge, memory, receipt = system
    identity = store.caller_id(NUMBER)
    store.replace_notes('default', identity, 'Prefers afternoon calls.')
    events = [event('Delivery is arriving tomorrow.'), event('RAM replacement is ordered.', 'e2')]
    await memory.checkpoint(receipt['id'], events)
    # Checkpoints are not raw dialogue copies and don't yet replace notes.
    assert store.read('default', identity) == 'Prefers afternoon calls.'
    store.revoke('binding')
    memory.ended('binding')
    with pytest.raises(PermissionError):
        store.update_for_session('binding', 'late caller tool')
    result = await memory.finalize(receipt['id'])
    notes = store.read('default', identity)
    tomorrow = (datetime.fromtimestamp(events[0]['timestamp'], ZoneInfo('Asia/Kolkata')).date() + timedelta(days=1)).isoformat()
    assert result['status'] == 'saved'
    assert 'Prefers afternoon calls.' in notes and 'RAM replacement is ordered.' in notes
    assert tomorrow in notes and 'tomorrow' not in notes and 'call call' in notes
    assert 'Caller-reported fact' in notes
    assert await memory.finalize(receipt['id']) == result
    await memory.checkpoint(receipt['id'], events)  # lost acknowledgement replay
    assert store.read('default', identity) == notes
    assert not memory._load(receipt['id'])['updates']


async def test_restart_reuses_durable_checkpoint_and_receipt(system):
    store, bridge, memory, receipt = system
    await memory.checkpoint(receipt['id'], [event('RAM is being replaced.')])
    restarted = CallMemory(bridge, generate=extract)
    assert (await restarted.finalize(receipt['id']))['status'] == 'saved'
    version = store.snapshot('default', store.caller_id(NUMBER))['revision']
    restarted_again = CallMemory(bridge, generate=extract)
    assert (await restarted_again.finalize(receipt['id']))['status'] == 'saved'
    assert store.snapshot('default', store.caller_id(NUMBER))['revision'] == version


async def test_merge_retries_concurrent_owner_edit_without_loss(system):
    store, bridge, memory, receipt = system
    identity = store.caller_id(NUMBER)
    store.replace_notes('default', identity, 'Original note.')
    await memory.checkpoint(receipt['id'], [event('Delivery is booked.')])
    async def concurrent(messages):
        store.replace_notes('default', identity, 'Original note. Owner added invoice details.')
        return await extract(messages)
    memory.generate = concurrent
    assert (await memory.finalize(receipt['id']))['status'] == 'pending'
    assert store.read('default', identity) == 'Original note. Owner added invoice details.'
    memory.generate = extract
    await memory._commit(receipt['id'])
    assert 'Owner added invoice details.' in store.read('default', identity)
    assert 'Delivery is booked.' in store.read('default', identity)


async def test_forget_cancels_pending_facts_and_late_extraction(system):
    store, bridge, memory, receipt = system
    async def forget_while_extracting(messages):
        store.forget('default', store.caller_id(NUMBER))
        return await extract(messages)
    memory.generate = forget_while_extracting
    with pytest.raises(PermissionError):
        await memory.checkpoint(receipt['id'], [event('Do not restore this fact.')])
    assert store.read('default', store.caller_id(NUMBER)) == ''
    with pytest.raises(PermissionError):
        memory.authenticated(receipt['id'], receipt['token'])


async def test_storage_failure_rolls_back_note_and_receipt(system, monkeypatch):
    store, bridge, memory, receipt = system
    await memory.checkpoint(receipt['id'], [event('Delivery scheduled.')])
    original = store._replace_notes
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError('disk failure')
    monkeypatch.setattr(store, '_replace_notes', fail_after_write)
    assert (await memory.finalize(receipt['id']))['status'] == 'pending'
    assert store.read('default', store.caller_id(NUMBER)) == ''
    monkeypatch.setattr(store, '_replace_notes', original)
    await memory._commit(receipt['id'])
    assert memory._load(receipt['id'])['status'] == 'saved'
    assert store.read('default', store.caller_id(NUMBER)).count('Delivery scheduled.') == 1


async def test_capacity_failure_keeps_existing_notes(system):
    store, bridge, memory, receipt = system
    store.replace_notes('default', store.caller_id(NUMBER), 'a' * 7990)
    await memory.checkpoint(receipt['id'], [event('New useful information.')])
    result = await memory.finalize(receipt['id'])
    assert result['status'] == 'failed' and result['reason'] == 'notes_capacity_exceeded'
    assert store.read('default', store.caller_id(NUMBER)) == 'a' * 7990


@pytest.mark.parametrize('number,remember,tools,reason', [
    (NUMBER, False, ['hfp_caller_update'], 'memory_disabled'),
    (None, True, ['hfp_caller_update'], 'memory_disabled'),
    (NUMBER, True, ['hfp_caller_read'], 'write_denied'),
])
def test_memory_permission_is_required(system, number, remember, tools, reason):
    store, bridge, memory, receipt = system
    store.bind(session_id='other', call_id='other', profile='default', number=number,
               policy={'admin': False, 'remember': remember, 'tools': tools}, ttl=100, remember=remember)
    result = memory.begin('other', number=number, outbound=False, timezone='UTC')
    assert result['status'] == 'skipped' and result['reason'] == reason and 'token' not in result


async def test_policy_change_and_expired_authority_cannot_commit(system):
    store, bridge, memory, receipt = system
    await memory.checkpoint(receipt['id'], [event('Private fact.')])
    data = routing_data()
    data['policies']['owner']['remember'] = False
    bridge.config = RoutingConfig.parse(data)
    with pytest.raises(PermissionError, match='permission_changed'):
        await memory.finalize(receipt['id'])
    assert store.read('default', store.caller_id(NUMBER)) == ''
    bridge.config = RoutingConfig.parse(routing_data())
    with store._lock, store.db:
        row = memory._load(receipt['id'])
        row['deadline'] = time.time() - 1
        memory._save(row)
    with pytest.raises(PermissionError, match='closeout_expired'):
        await memory.finalize(receipt['id'])


async def test_no_useful_updates_and_incomplete_capture_are_distinct(system):
    store, bridge, memory, receipt = system
    async def empty(messages):
        return {'updates': []}
    memory.generate = empty
    await memory.checkpoint(receipt['id'], [event('Hello')])
    result = await memory.finalize(receipt['id'], complete=False)
    assert result['status'] == 'failed' and result['reason'] == 'incomplete_capture'


async def test_only_caller_evidence_can_support_updates(system):
    store, bridge, memory, receipt = system
    async def invented(messages):
        return {'updates': [dict(text='Payment completed', source_event_id='assistant', kind='fact', due_date=None)]}
    memory.generate = invented
    with pytest.raises(ValueError, match='invalid extracted fact'):
        await memory.checkpoint(receipt['id'], [event('I could pay for it.', 'assistant', 'output')])
    assert memory._load(receipt['id'])['updates'] == []


async def test_write_only_extraction_never_receives_old_notes(system):
    store, bridge, memory, receipt = system
    data = routing_data()
    data['policies']['owner'] = {'tools': ['hfp_caller_update']}
    bridge.config = RoutingConfig.parse(data)
    policy = asdict(bridge.config.policies['owner']); policy['tools'] = sorted(policy['tools'])
    store.bind(session_id='write', call_id='write', profile='default', number=NUMBER, policy=policy, ttl=100)
    store.replace_notes('default', store.caller_id(NUMBER), 'Secret prior note')
    receipt = memory.begin('write', number=NUMBER, outbound=False, timezone='UTC')
    async def inspected(messages):
        assert 'Secret prior note' not in json.dumps(messages)
        return await extract(messages)
    memory.generate = inspected
    await memory.checkpoint(receipt['id'], [event('New caller fact')])
    assert (await memory.finalize(receipt['id']))['status'] == 'saved'
    assert 'Secret prior note' in store.read('default', store.caller_id(NUMBER))


async def test_event_replays_and_changed_event_identity(system):
    store, bridge, memory, receipt = system
    first, second = event('Delivery tomorrow.'), event('RAM ordered.', 'e2')
    await memory.checkpoint(receipt['id'], [first])
    await memory.checkpoint(receipt['id'], [first, second])
    assert len(memory._load(receipt['id'])['updates']) == 2
    with pytest.raises(ValueError, match='identity changed'):
        await memory.checkpoint(receipt['id'], [{**first, 'text': 'forged'}])


def test_notes_revisions_across_connections_and_forget(system):
    store, bridge, memory, receipt = system
    identity = store.caller_id(NUMBER)
    other = CallerStore(store.path)
    try:
        initial = store.snapshot('default', identity)
        other.replace_notes('default', identity, 'owner update', expected_revision=initial['revision'])
        with pytest.raises(NoteConflict):
            store.replace_notes('default', identity, 'stale call update', expected_revision=initial['revision'])
        before = store.snapshot('default', identity)
        other.forget('default', identity)
        with pytest.raises(NoteConflict):
            store.replace_notes('default', identity, 'resurrection', expected_revision=before['revision'])
    finally:
        other.close()


@pytest.mark.parametrize('retain', [True, False])
async def test_controller_abrupt_hangup_keeps_notes_and_optional_full_transcript(system, tmp_path, retain):
    store, bridge, memory, receipt = system
    ledger = RequestLedger(tmp_path / 'calls.db')
    operations = []
    class API:
        def __init__(self, endpoint): pass
        async def memory(self, receipt, action, **kw):
            if action == 'checkpoint': return await memory.checkpoint(receipt['id'], kw['events'])
            if action == 'finalize': return await memory.finalize(receipt['id'], complete=kw['capture_complete'])
            return memory.authenticated(receipt['id'], receipt['token'])
        async def revoke(self, sid):
            store.revoke(sid); memory.ended(sid); operations.append('revoked')
        async def stop(self): pass
        async def close(self): pass
    async def noop(*args): pass
    c = PhoneController(bridge.config, snapshot=lambda: {}, answer=noop, end=noop, make_voice=None,
                        api_factory=API, ledger=ledger, full_transcripts=retain)
    c.call_id = 'call'; c.route = bridge.config.numbers[NUMBER]
    c.binding = {'session_id': 'binding'}; c.api = API(None)
    c.memory_capture = new_capture(c, receipt, retain_transcripts=retain)
    capture = c.memory_capture
    c._track_memory(capture)
    live = GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None,
                            full_transcripts_enabled=retain, transcript_sink=ledger.append_transcript,
                            memory_sink=c.capture_memory)
    live._session_id = 'call'
    live.add_transcript('input', 'The delivery will arrive tomorrow.')
    live.append_transcript_fragment('input', 'The RAM replacement is ordered.')
    class Voice:
        async def stop(self):
            assert operations == ['revoked']
            live.flush_transcript_fragments()
    c.voice = Voice()
    ledger.save_call_summary('call', 'Call ended.')
    await c.finish()
    await asyncio.wait_for(capture.task, 2)
    notes = store.read('default', store.caller_id(NUMBER))
    assert 'delivery' in notes and 'RAM replacement' in notes
    assert 'tomorrow' not in notes
    assert ledger.get_call_summary('call')['memory_save']['status'] == 'saved'
    assert c.status['memory_save']['status'] == 'saved'
    transcript = ledger.transcript('call')['events']
    assert len(transcript) == (2 if retain else 0)
    if retain:
        assert transcript[0]['text'] == 'The delivery will arrive tomorrow.'
    else:
        assert live.get_call_transcript('call')['events'] == []
    ledger.close()


async def test_gateway_operations_require_both_credentials_and_ignore_forged_identity(system, monkeypatch):
    store, bridge, memory, receipt = system
    monkeypatch.setenv('TEST_BRIDGE_TOKEN', 'b' * 40)
    app = web.Application()
    bridge.wire(app)
    bridge.memory.generate = extract
    async with TestClient(TestServer(app)) as client:
        url = f"/v1/hfp/memory/{receipt['id']}/checkpoint"
        body = {'token': receipt['token'], 'events': [event('Delivery confirmed.')]}
        assert (await client.post(url, json=body)).status == 401
        headers = {'X-HFP-Bridge-Token': 'b' * 40}
        assert (await client.post(url, headers=headers, json={**body, 'token': 'wrong'})).status == 403
        assert (await client.post(url, headers=headers, json={**body, 'number': '+919876543211'})).status == 400
        assert (await client.post(url, headers=headers, json=body)).status == 200
        response = await client.post('/v1/hfp/caller-notes/update', headers=headers,
                                     json={'number': NUMBER, 'notes': 'owner'})
        assert response.status == 400
        response = await client.post('/v1/hfp/caller-notes/update', headers=headers,
                                     json={'number': NUMBER, 'notes': 'owner', 'expected_revision': 0})
        assert response.status == 200
        response = await client.post('/v1/hfp/caller-notes/update', headers=headers,
                                     json={'number': NUMBER, 'notes': 'stale', 'expected_revision': 0})
        assert response.status == 409


async def test_daemon_restart_recovers_saved_transcript_events(system, tmp_path):
    store, bridge, memory, receipt = system
    ledger = RequestLedger(tmp_path / 'calls.db')
    async def noop(*args): pass
    c = PhoneController(bridge.config, snapshot=lambda: {}, answer=noop, end=noop, make_voice=None, ledger=ledger)
    c.call_id = 'call'; c.route = bridge.config.numbers[NUMBER]
    capture = new_capture(c, receipt, retain_transcripts=True)
    captured = {**event('RAM replacement Friday.'), 'session_id': 'call', 'provider': 'test'}
    ledger.append_transcript(captured)
    capture.capture(captured)
    capture.end()
    restored = recover(c, transcripts_enabled=True)
    assert len(restored) == 1
    assert list(restored[0].events) == [{k: captured[k] for k in ('event_id', 'direction', 'text', 'timestamp')}]
    assert restored[0].data['capture_complete'] is True
    ledger.close()


@pytest.mark.parametrize('voice_mode', ['classic', 'gemini_live'])
@pytest.mark.parametrize('continuity', [False, True])
@pytest.mark.parametrize('outbound', [False, True])
async def test_admitted_route_memory_independent_of_voice_and_continuity(system, voice_mode, continuity, outbound):
    store, bridge, memory, receipt = system
    data = routing_data()
    data['default'] = {'endpoint': 'owner', 'policy': 'guest', 'voice': voice_mode, 'continuity': continuity}
    if outbound:
        data.pop('default')  # Exercise the restricted outbound fallback.
    bridge.config = RoutingConfig.parse(data)
    number = '+919876543211'
    route, _ = bridge.config.resolve_outbound(number) if outbound else bridge.config.resolve(number)
    policy = asdict(bridge.config.policies[route.policy]); policy['tools'] = sorted(policy['tools'])
    store.bind(session_id='guest', call_id='guest', profile='default', number=number, policy=policy, ttl=100)
    receipt = memory.begin('guest', number=number, outbound=outbound, timezone='UTC')
    await memory.checkpoint(receipt['id'], [event('Delivery scheduled for Friday.')])
    assert (await memory.finalize(receipt['id']))['status'] == 'saved'
    assert 'Delivery scheduled' in store.read('default', store.caller_id(number))
    assert store.read('default', store.caller_id(NUMBER)) == ''
    assert store.read('another-profile', store.caller_id(number)) == ''


async def test_classic_flushes_received_audio_after_hangup(system, tmp_path):
    from hfp_mcp.voice_backends import ClassicVoice
    from unittest.mock import AsyncMock
    store, bridge, memory, receipt = system
    ledger = RequestLedger(tmp_path / 'calls.db')
    async def noop(*args): pass
    c = PhoneController(bridge.config, snapshot=lambda: {}, answer=noop, end=noop, make_voice=None,
                        ledger=ledger, transcript_sink=ledger.append_transcript)
    c.call_id = 'call'; c.route = bridge.config.numbers[NUMBER]
    c.memory_capture = new_capture(c, receipt, retain_transcripts=True)
    c.api = SimpleNamespace(request=AsyncMock(return_value={'text': 'The RAM is replaced.'}))
    voice = ClassicVoice(c, acquire=None, release=None, clear=None)
    voice.tail_audio = b'\0' * 5000
    store.revoke('binding'); memory.ended('binding')
    c.call_id = None
    await voice.stop()
    assert ledger.transcript('call')['events'][0]['text'] == 'The RAM is replaced.'
    await memory.checkpoint(receipt['id'], list(c.memory_capture.events))
    assert (await memory.finalize(receipt['id']))['status'] == 'saved'
    ledger.close()


async def test_midnight_dates_use_each_statement_not_finalization(system):
    store, bridge, memory, receipt = system
    first = datetime(2026, 9, 21, 23, 59, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
    second = first + 120
    with store._lock, store.db:
        row = memory._load(receipt['id']); row['started_at'] = first - 1; memory._save(row)
    await memory.checkpoint(receipt['id'], [event('Delivery tomorrow.', 'first', timestamp=first),
                                           event('RAM replacement tomorrow.', 'second', timestamp=second)])
    result = await memory.finalize(receipt['id'])
    assert result['status'] == 'saved'
    notes = store.read('default', store.caller_id(NUMBER))
    assert 'Delivery 2026-09-22.' in notes and 'RAM replacement 2026-09-23.' in notes


async def test_manual_save_before_closeout_does_not_duplicate(system):
    store, bridge, memory, receipt = system
    await memory.checkpoint(receipt['id'], [event('RAM replacement ordered.')])
    store.update_for_session('binding', 'RAM replacement ordered.', expected_revision=0)
    result = await memory.finalize(receipt['id'])
    assert result['status'] == 'saved'
    note = store.read('default', store.caller_id(NUMBER))
    assert note.count('RAM replacement ordered.') == 1
    assert 'Asia/Kolkata' in note and 'call call' in note


async def test_gateway_worker_retries_sealed_work_after_restart(system, monkeypatch):
    store, bridge, memory, receipt = system
    await memory.checkpoint(receipt['id'], [event('Delivery scheduled.')])
    original = store._replace_notes
    monkeypatch.setattr(store, '_replace_notes', lambda *a, **kw: (_ for _ in ()).throw(OSError('unavailable')))
    assert (await memory.finalize(receipt['id']))['status'] == 'pending'
    monkeypatch.setattr(store, '_replace_notes', original)
    restarted = CallMemory(bridge, generate=extract)
    await restarted.start()
    try:
        async with asyncio.timeout(5):
            while restarted._load(receipt['id'])['status'] == 'pending':
                await asyncio.sleep(.02)
        assert restarted._load(receipt['id'])['status'] == 'saved'
    finally:
        await restarted.close()


async def test_model_adapter_has_no_tools_and_never_uses_reasoning(system, monkeypatch):
    import sys
    from contextlib import nullcontext
    from hfp_mcp.call_memory import model_json
    seen = []
    async def response(**kwargs):
        seen.append(kwargs)
        return {'choices': [{'message': {'content': '{"updates": []}', 'reasoning': 'private'}}]}
    monkeypatch.setitem(sys.modules, 'agent', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'agent.auxiliary_client', SimpleNamespace(async_call_llm=response))
    adapter = SimpleNamespace(_profile_scope=lambda profile: nullcontext())
    assert await model_json(adapter, 'default', []) == {'updates': []}
    assert seen[0]['tools'] == [] and seen[0]['task'] == 'phone_memory'
    async def reasoning_only(**kwargs):
        return {'choices': [{'message': {'content': None, 'reasoning': '{"updates": []}'}}]}
    monkeypatch.setitem(sys.modules, 'agent.auxiliary_client', SimpleNamespace(async_call_llm=reasoning_only))
    with pytest.raises(ValueError):
        await model_json(adapter, 'default', [])


async def test_recovery_does_not_borrow_another_routes_continuity(system, tmp_path):
    store, bridge, memory, receipt = system
    data = routing_data()
    data['default'] = {'endpoint': 'owner', 'policy': 'guest', 'continuity': True}
    config = RoutingConfig.parse(data)
    ledger = RequestLedger(tmp_path / 'calls.db')
    async def noop(*args): pass
    c = PhoneController(config, snapshot=lambda: {}, answer=noop, end=noop, make_voice=None, ledger=ledger)
    c.call_id = 'call'; c.route = config.numbers[NUMBER]; c._number = NUMBER
    capture = new_capture(c, receipt, retain_transcripts=True)
    captured = {**event('Private retained text.'), 'session_id': 'call', 'provider': 'test'}
    capture.capture(captured); ledger.append_transcript(captured); capture.end()
    restored = recover(c, transcripts_enabled=False)
    assert len(restored) == 1 and not restored[0].events
    assert not restored[0].data['capture_complete']
    ledger.close()


async def test_checkpoint_overlap_supplies_context_but_cannot_duplicate_evidence(system):
    store, bridge, memory, receipt = system
    first = event('Will you deliver the RAM tomorrow?', 'assistant', 'output')
    await memory.checkpoint(receipt['id'], [first])
    second = event('Yes, I agree.', 'caller')
    async def inspect(messages):
        body = json.loads(messages[-1]['content'])
        assert body['events'][0]['text'] == first['text']
        assert body['new_event_ids'] == ['caller']
        return {'updates': [dict(text='Agreed to deliver the RAM tomorrow.', source_event_id='caller', kind='commitment', due_date=None)]}
    memory.generate = inspect
    await memory.checkpoint(receipt['id'], [first, second])
    assert (await memory.finalize(receipt['id']))['status'] == 'saved'
    assert 'Caller-reported commitment' in store.read('default', store.caller_id(NUMBER))


def test_transcription_uses_first_fragment_time_even_when_flushed_next_day(monkeypatch):
    from hfp_mcp import live_ai
    captured = []
    live = GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None, memory_sink=captured.append)
    live._session_id = 'call'
    before_midnight = datetime(2026, 9, 21, 23, 59, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
    monkeypatch.setattr(live_ai.time, 'time', lambda: before_midnight)
    live.append_transcript_fragment('input', 'Delivery tomorrow')
    monkeypatch.setattr(live_ai.time, 'time', lambda: before_midnight + 120)
    live.flush_transcript_fragments()
    assert captured[0]['timestamp'] == before_midnight


async def test_deduplication_preserves_currency_and_changed_due_date(system):
    store, bridge, memory, receipt = system
    async def different(messages):
        return {'updates': [
            dict(text='Quoted $100.', source_event_id='a', kind='fact', due_date=None),
            dict(text='Quoted ₹100.', source_event_id='a', kind='fact', due_date=None),
            dict(text='Agreed delivery.', source_event_id='a', kind='commitment', due_date='2026-09-23'),
            dict(text='Agreed delivery.', source_event_id='a', kind='commitment', due_date='2026-09-24'),
        ]}
    memory.generate = different
    await memory.checkpoint(receipt['id'], [event('A report with changed terms.', 'a')])
    updates = memory._load(receipt['id'])['updates']
    assert len(updates) == 4 and len({u['id'] for u in updates}) == 4
