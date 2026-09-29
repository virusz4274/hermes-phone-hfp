import asyncio
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace, ModuleType
import sys

import pytest
import httpx

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.phone_tasks import GatewayTasks, TERMINAL
from hfp_mcp.routing import RoutingConfig
from hfp_mcp.phone_controls import literal_hangup
from tests.test_phone_routing import routing_data


class Native:
    def __init__(self):
        self.runs = {}
        self.submissions = []
        self.stopped = []
        self.corrections = []
    async def request(self, method, path, **kw):
        if path == 'v1/runs':
            self.submissions.append(kw)
            run = 'run-' + str(len(self.submissions))
            headers, root = kw['headers'], kw['json']['session_id']
            self.manager.admitted(headers['X-HFP-Task'], run, headers['X-HFP-Binding'], root)
            self.manager.store.bind_run(run, headers['X-HFP-Binding'], root)
            self.runs[run] = {'run_id':run, 'status':'running'}
            return {'run_id':run}
        return dict(self.runs[path.split('/')[2]])
    async def stop(self, run):
        self.stopped.append(run)
        self.runs[run]['status'] = 'cancelled'
    async def steer(self, run, text):
        if self.runs[run]['status'] != 'running': raise ValueError('not accepting')
        self.corrections.append((run,text))
        return {'accepted': True}
    async def close(self): pass


@pytest.fixture
def system(tmp_path, monkeypatch):
    data = routing_data()
    data['policies']['owner']['background_tasks'] = True
    config = RoutingConfig.parse(data)
    store = CallerStore(tmp_path/'callers.sqlite3')
    policy = {'admin':True, 'background_tasks':True, 'tools':[], 'remember':True}
    store.bind(session_id='parent', call_id='call', profile='default', number='+919876543210', policy=policy, ttl=60)
    root = store.manage('a'*32, 'parent')
    api = Native()
    manager = GatewayTasks(SimpleNamespace(store=store, config=config, profile='default'),
        SimpleNamespace(_profile_scope=lambda _:nullcontext()), [], asyncio.Lock(), api=api)
    manager.peers.append(manager)
    api.manager = manager
    monkeypatch.setattr('hfp_mcp.hermes_sessions.create', lambda *a: None)
    monkeypatch.setattr(manager, 'continuation_allowed', lambda binding: None if binding['policy']['admin'] else (_ for _ in ()).throw(PermissionError()))
    yield manager, api, store, root
    store.close()


async def submit(m, key, **kw):
    result = await m.submit(dict(binding_id='parent', conversation_id='a'*32,
        request_id=key, text='Do requested work', **kw))
    await asyncio.sleep(0)
    return m.registry.get(result['task_id']) if result.get('task_id') else result


async def test_default_long_task_reuses_primary_overlap_lazy_and_capacity(system):
    m, api, store, root = system
    first = await submit(m, 'one', continue_after_call=True)
    assert first['session_id'] == root
    assert not m.registry.roots('a'*32)
    second = await submit(m, 'two', relationship='independent')
    assert second['session_id'] != root
    assert len(m.registry.roots('a'*32)) == 1
    third = await submit(m, 'three', relationship='independent')
    assert third['status'] == 'busy' and len(api.submissions) == 2
    assert store.run_binding(first['run_id'])['root_id'] == root
    assert store.run_binding(second['run_id'])['root_id'] == second['session_id']
    api.runs[first['run_id']].update(status='completed', output='done')
    await m.refresh(first)
    next_task = await submit(m, 'four', relationship='independent')
    assert next_task['session_id'] == root
    assert 'conversation_history' not in api.submissions[-1]['json']


async def test_correction_followup_status_and_replacement_do_not_duplicate(system):
    m, api, store, root = system
    one = await submit(m, 'one')
    with pytest.raises(ValueError): await submit(m, 'ambiguous')
    await submit(m, 'correct', relationship='correction', task_id=one['task_id'])
    assert len(api.corrections) == 1 and len(api.submissions) == 1
    api.runs[one['run_id']].update(status='completed', output='done')
    status = await m.control({'binding_id':'parent','conversation_id':'a'*32,'action':'status'})
    assert status['tasks'][0]['status'] == 'completed'
    follow = await submit(m, 'follow', relationship='follow_up', task_id=one['task_id'])
    assert follow['session_id'] == root
    result = await submit(m, 'replace', relationship='replace', task_id=follow['task_id'])
    assert result['status'] == 'stopping'
    assert len(api.submissions) == 2 and api.stopped == [follow['run_id']]


async def test_late_correction_continues_once_after_hangup_and_reports_updated_result(system):
    m, api, store, root = system
    original = await submit(m, 'reminder', continue_after_call=True)
    correction = 'Change the existing 6 PM callback to 4:30 PM.'
    await m.control(dict(binding_id='parent', conversation_id='a'*32,
        action='steer', task_id=original['task_id'], text=correction))
    api.runs[original['run_id']].update(status='completed', output='Scheduled for 6 PM', pending_steer=correction)
    store.revoke('parent')

    # Competing status polls must not launch the correction more than once or
    # expose the stale 6 PM result as the task's final successful outcome.
    results = await asyncio.gather(m.refresh(original), m.refresh(original))
    await asyncio.sleep(0)
    assert all(r['status'] not in TERMINAL for r in results)
    assert all(r['message'] != 'Scheduled for 6 PM' for r in results)
    assert len(api.submissions) == 2
    follow = m.registry.get(original['task_id'])
    assert follow['run_id'] != original['run_id']
    assert follow['session_id'] == root
    assert follow['continue_after_call'] is True
    assert follow['expires_at'] == original['expires_at']
    assert follow['delivery'] == 'pending'
    assert follow['run_history'][0]['run_id'] == original['run_id']
    assert follow['run_history'][0]['output'] == 'Scheduled for 6 PM'
    envelope = json.loads(api.submissions[-1]['json']['input'])
    assert envelope['caller_correction'] == correction
    assert envelope['previous_run_output'] == 'Scheduled for 6 PM'
    assert api.submissions[0]['headers']['Idempotency-Key'] != api.submissions[1]['headers']['Idempotency-Key']
    assert store.run_binding(follow['run_id'])
    with pytest.raises(PermissionError): store.run_binding(original['run_id'])
    assert m.capacity.db.execute('SELECT count(*) FROM phone_task_slots').fetchone()[0] == 1

    # Polls holding the old snapshot must now observe the follow-up run.
    api.runs[follow['run_id']].update(status='completed', output='Verified callback and reminder at 4:30 PM')
    final = await m.refresh(original)
    assert final['status'] == 'completed' and '4:30 PM' in final['message']
    assert final['pending_steer'] is None
    assert len(api.submissions) == 2
    assert m.capacity.db.execute('SELECT count(*) FROM phone_task_slots').fetchone()[0] == 0


@pytest.mark.parametrize('reason', ['expired', 'hangup', 'cancelled', 'failed', 'interrupted', 'reset', 'stop'])
async def test_pending_correction_never_revives_ended_authority_or_failed_work(system, reason):
    m, api, store, root = system
    original = await submit(m, 'one', continue_after_call=reason != 'hangup')
    if reason == 'expired': m.registry.update(original['task_id'], expires_at=time.time()-1)
    if reason == 'hangup': store.revoke('parent')
    if reason == 'reset': store.retire_managed(root)
    if reason == 'stop': await m.stop(original)
    status = reason if reason in {'cancelled', 'failed', 'interrupted'} else 'completed'
    api.runs[original['run_id']].update(status=status, output='Original result', pending_steer='Change the time')
    result = await m.refresh(original)
    await asyncio.sleep(0)
    assert len(api.submissions) == 1
    assert result['status'] in TERMINAL - {'completed'}
    assert result['pending_steer'] == 'Change the time'
    assert 'correction was not applied' in result['message']
    with pytest.raises(PermissionError): store.run_binding(original['run_id'])


async def test_rejected_steer_completion_race_starts_only_the_correction(system, monkeypatch):
    m, api, store, root = system
    original = await submit(m, 'one', continue_after_call=True)
    api.runs[original['run_id']].update(status='completed', output='Original result')
    async def rejected(*args):
        response = httpx.Response(409, request=httpx.Request('POST', 'http://test/steer'))
        raise httpx.HTTPStatusError('not accepting', request=response.request, response=response)
    monkeypatch.setattr(api, 'steer', rejected)
    result = await m.control(dict(binding_id='parent', conversation_id='a'*32,
        action='steer', task_id=original['task_id'], text='Change the time'))
    await asyncio.sleep(0)
    assert result['status'] == 'submitting'
    assert len(api.submissions) == 2
    assert json.loads(api.submissions[-1]['json']['input'])['caller_correction'] == 'Change the time'


async def test_steer_timeout_never_resubmits_ambiguous_input(system, monkeypatch):
    m, api, store, root = system
    original = await submit(m, 'one')
    async def timeout(*args): raise TimeoutError('unknown delivery')
    monkeypatch.setattr(api, 'steer', timeout)
    with pytest.raises(TimeoutError):
        await m.control(dict(binding_id='parent', conversation_id='a'*32,
            action='steer', task_id=original['task_id'], text='Change the time'))
    assert len(api.submissions) == 1


async def test_restart_during_followup_admission_never_replays_it(system, monkeypatch):
    m, api, store, root = system
    original = await submit(m, 'one', continue_after_call=True)
    api.runs[original['run_id']].update(status='completed', output='Original result', pending_steer='Change the time')
    pending_launches = []
    monkeypatch.setattr(m, 'spawn', lambda coro: pending_launches.append(coro))
    follow = await m.refresh(original)
    assert follow['status'] == 'submitting' and follow['run_id'] is None
    for coro in pending_launches: coro.close()
    m2 = GatewayTasks(m.bridge, m.adapter, m.peers, m.admission_lock, api=api)
    await m2.start()
    await m2.close()
    row = m2.registry.get(original['task_id'])
    assert row['status'] == 'uncertain' and row['pending_steer'] == 'Change the time'
    assert len(api.submissions) == 1
    with pytest.raises(PermissionError): store.binding(follow['binding_id'])


async def test_completion_notification_waits_for_correction_result(system, monkeypatch):
    m, api, store, root = system
    original = await submit(m, 'reminder', continue_after_call=True)
    api.runs[original['run_id']].update(status='completed', output='Scheduled for 6 PM',
        pending_steer='Move it to 4:30 PM')
    notices = []
    delivered = asyncio.Event()
    async def deliver(row):
        notices.append(row['message'])
        m.registry.update(row['task_id'], delivery='sent')
        delivered.set()
    monkeypatch.setattr(m, 'deliver', deliver)
    await m.start()
    try:
        for _ in range(100):
            if len(api.submissions) == 2: break
            await asyncio.sleep(.01)
        assert len(api.submissions) == 2 and notices == []
        follow = m.registry.get(original['task_id'])
        api.runs[follow['run_id']].update(status='completed', output='Verified 4:30 PM')
        await asyncio.wait_for(delivered.wait(), 3)
        assert notices == ['Verified 4:30 PM']
    finally:
        await m.close()


async def test_cancellation_with_stale_snapshot_stops_followup(system):
    m, api, store, root = system
    original = await submit(m, 'reminder', continue_after_call=True)
    api.runs[original['run_id']].update(status='completed', output='Original result',
        pending_steer='Change the time')
    await m.refresh(original)
    await asyncio.sleep(0)
    follow = m.registry.get(original['task_id'])
    await m.stop(original)
    assert api.stopped == [follow['run_id']]
    with pytest.raises(PermissionError): store.run_binding(follow['run_id'])
    final = await m.refresh(original)
    assert final['status'] == 'cancelled'


async def test_followup_admission_timeout_is_not_retried_by_status_or_request_replay(system, monkeypatch):
    m, api, store, root = system
    original = await submit(m, 'one', continue_after_call=True)
    api.runs[original['run_id']].update(status='completed', output='Original result', pending_steer='Change the time')
    native_request = api.request
    attempts = []
    async def request(method, path, **kw):
        if path == 'v1/runs':
            attempts.append(kw)
            raise TimeoutError('admission outcome unknown')
        return await native_request(method, path, **kw)
    monkeypatch.setattr(api, 'request', request)
    await m.refresh(original)
    await asyncio.sleep(0)
    replay = await submit(m, 'one', continue_after_call=True)
    status = await m.refresh(original)
    assert replay['status'] == status['status'] == 'uncertain'
    assert len(attempts) == 1
    assert status['pending_steer'] == 'Change the time'


async def test_fresh_authority_hangup_expiry_and_revoke_individually(system):
    m, api, store, root = system
    one = await submit(m, 'one', continue_after_call=True)
    two = await submit(m, 'two', relationship='independent')
    store.revoke('parent')
    assert store.run_binding(one['run_id'])
    with pytest.raises(PermissionError): store.run_binding(two['run_id'])
    m.registry.update(one['task_id'], expires_at=time.time()-1)
    with pytest.raises(PermissionError): store.run_binding(one['run_id'])
    await m.stop(one)
    assert api.stopped == [one['run_id']]
    with pytest.raises(PermissionError): store.run_binding(one['run_id'])


async def test_isolation_and_idempotency(system):
    m, api, store, root = system
    one = await submit(m,'one')
    replay = await submit(m,'one')
    assert replay['run_id'] == one['run_id'] and len(api.submissions) == 1
    with pytest.raises(ValueError): await submit(m,'one',label='changed')
    store.bind(session_id='bob',call_id='other',profile='default',number='+919876543211',policy={'admin':True},ttl=60)
    with pytest.raises(PermissionError):
        await m.control({'binding_id':'bob','conversation_id':'a'*32,'action':'status'})
    with pytest.raises(PermissionError):
        await m.control({'binding_id':'parent','conversation_id':'a'*32,'action':'status','task_id':'invented'})


async def test_restart_reconciles_never_resubmits_and_keeps_callback_result(system):
    m, api, store, root = system
    one = await submit(m, 'one', continue_after_call=True)
    api.runs[one['run_id']].update(status='interrupted',error='gateway restarted')
    m2 = GatewayTasks(m.bridge,m.adapter,m.peers,m.admission_lock,api=api)
    result = await m2.control({'binding_id':'parent','conversation_id':'a'*32,'action':'status'})
    assert result['tasks'][0]['status'] == 'interrupted'
    assert len(api.submissions) == 1
    with pytest.raises(PermissionError): store.run_binding(one['run_id'])


async def test_notification_timeout_no_resend_or_custom_transport(system, monkeypatch):
    m, api, store, root = system
    one = await submit(m, 'one', continue_after_call=True)
    module = ModuleType('tools.send_message_tool')
    sent = []
    def send(args):
        sent.append(args)
        raise TimeoutError('uncertain')
    module.send_message_tool = send
    monkeypatch.setitem(sys.modules,'tools.send_message_tool',module)
    api.runs[one['run_id']].update(status='completed',output='result MEDIA:/unexpected/path')
    await m.start()
    await asyncio.sleep(1.1)
    await m.close()
    assert len(sent) == 1
    assert sent[0]['target'] == 'telegram'
    assert 'MEDIA:' not in sent[0]['message']
    assert m.registry.get(one['task_id'])['delivery'] == 'uncertain'
    await m.start()
    await asyncio.sleep(.05)
    await m.close()
    assert len(sent) == 1


@pytest.mark.parametrize('text', ['Disconnect the call.', 'We can cut the call. Disconnect the call.',
    'Please hang up now.', 'കോൾ കട്ട് ചെയ്യൂ'])
def test_real_hangup_fallback(text):
    assert literal_hangup(text)
    assert not literal_hangup(text, fictional=True)


@pytest.mark.parametrize('text', ["Don't disconnect the call", 'Do not hang up', 'Can you explain hang up?',
    'If I say disconnect the call', 'He said "disconnect the call"', 'Pretend to hang up',
    'The character says hang up', 'Hang up?', "Please don't hang up", 'Do not cut the call. Disconnect the call.'])
def test_hangup_fallback_excludes_noncommands(text):
    assert not literal_hangup(text)


def test_capacity_is_atomic_across_gateway_process_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from hfp_mcp.phone_tasks import TaskCapacity
    path = tmp_path/'calls.db'
    gates = [TaskCapacity(path, name) for name in ('default', 'business', 'other')]
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda pair: pair[1].reserve(str(pair[0]), 2), enumerate(gates)))
    assert sum(results) == 2
    for i, gate in enumerate(gates):
        if results[i]: gate.release(str(i))
    assert gates[2].reserve('new', 2)
    for gate in gates: gate.db.close()


async def test_observer_hangup_stops_only_call_bound_and_expiry_stops_continued(system):
    m, api, store, root = system
    continued = await submit(m, 'long', continue_after_call=True)
    bound = await submit(m, 'short', relationship='independent')
    store.revoke('parent')
    await m.start()
    await asyncio.sleep(.05)
    assert bound['run_id'] in api.stopped
    assert continued['run_id'] not in api.stopped
    m.registry.update(continued['task_id'],expires_at=time.time()-1)
    await asyncio.sleep(1.05)
    await m.close()
    assert continued['run_id'] in api.stopped
    assert m.registry.get(continued['task_id'])['status'] == 'cancelled'


async def test_unknown_admission_replay_never_submits_again(system):
    m, api, store, root = system
    calls = []
    async def fail(*args,**kwargs):
        calls.append(args)
        raise TimeoutError()
    api.request = fail
    one = await submit(m,'uncertain')
    assert one['status'] == 'uncertain'
    again = await submit(m,'uncertain')
    assert again['status'] == 'uncertain' and len(calls) == 1
    with pytest.raises(PermissionError): store.binding(one['binding_id'])


async def test_reset_revokes_both_sessions_and_releases_capacity_after_settle(system):
    m, api, store, root = system
    one = await submit(m,'one',continue_after_call=True)
    two = await submit(m,'two',relationship='independent')
    for sid in [root,*m.registry.roots('a'*32)]: store.retire_managed(sid)
    await m.cancel_conversation('a'*32)
    await m.settle_conversation('a'*32)
    assert len(api.stopped) == 2
    for row in (one,two):
        with pytest.raises(PermissionError): store.run_binding(row['run_id'])
    assert m.capacity.db.execute('SELECT count(*) FROM phone_task_slots').fetchone()[0] == 0


def test_continuation_requires_admin_policy_and_known_caller(system):
    m, api, store, root = system
    for binding in ({'policy':{'admin':False,'background_tasks':True},'persistent':True},
                    {'policy':{'admin':True,'background_tasks':False},'persistent':True},
                    {'policy':{'admin':True,'background_tasks':True},'persistent':False}):
        with pytest.raises(PermissionError): GatewayTasks.continuation_allowed(m,binding)
