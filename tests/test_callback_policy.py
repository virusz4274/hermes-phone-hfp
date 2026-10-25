"""Callbacks use native schedules, saved policy, and call-scoped recipient authority."""
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hfp_mcp.gemini_live import _live_config, _function_declarations
from hfp_mcp.phone_messages import schedule_confirmation
from hfp_mcp.phone_workflows import WorkflowConflict
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_workflows import manager, schedule, deciding, decide
from tests.test_phone_routing import routing_data


def admit(manager, key):
    row = manager.store.get(key)
    execution = manager.jobs.fire(row, 'exec-' + str(len(manager.jobs.executions)))
    return manager.admit(key, row['cron_job_id'], execution)[:2]


def observed(manager, row, attempt, *, reason='busy', connected=False, uncertain=False, active=False):
    return manager.store.update_attempt(row['id'], attempt['id'], status='observing', outcome={
        'connection_state': 'connected' if connected else 'not_connected', 'termination_reason': reason,
        'uncertain': uncertain, 'still_active': active, 'ended_at': time.time(), 'evidence': [], 'verified_actions': []})


@pytest.mark.parametrize('delays', [[], [10, 30, 60], [1]])
def test_retry_policy_config(delays):
    data = routing_data()
    data['callbacks'] = {'retry_delays_minutes': delays}
    assert RoutingConfig.parse(data).callback_retry_delays == tuple(delays)


@pytest.mark.parametrize('value', [None, 'x', [], {'retry_delays_minutes': [True]},
    {'retry_delays_minutes': [-1]}, {'retry_delays_minutes': [0]}, {'retry_delays_minutes': [1.5]},
    {'retry_delays_minutes': '10'}, {'attempts': 4}])
def test_invalid_callback_policy(value):
    data = routing_data()
    data['callbacks'] = value
    with pytest.raises(ValueError, match='callbacks'):
        RoutingConfig.parse(data)


def test_four_attempts_anchored_to_end_and_no_routine_messages(manager, monkeypatch):
    clock = [datetime(2026, 10, 18, 10, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr('hfp_mcp.phone_workflows.time.time', lambda: clock[0])
    row = schedule(manager)
    assert row['max_attempts'] == 4
    # Changing defaults cannot change this chain's persisted policy.
    manager.bridge.config = replace(manager.bridge.config, callback_retry_delays=())
    end_times = []
    for index in range(4):
        clock[0] = row['run_at'] + 42  # account for ringing, anchored to its end
        row, attempt = admit(manager, row['id'])
        row, attempt = observed(manager, row, attempt, reason='busy' if index % 2 == 0 else 'no_answer')
        end_times.append(clock[0])
        assert manager.routine_decision(row['id'], attempt['id'])
        manager.finish_report(row['id'], attempt['id'])
        row = manager.store.get(row['id'])
        saved = row['attempts'][-1]
        if index < 3:
            assert row['run_at'] == end_times[-1] + [10, 30, 60][index] * 60
            assert row['status'] == 'scheduled' and saved['report'] == '' and saved['report_claimed']
        else:
            assert row['status'] == 'exhausted'
            assert saved['report'] == "I couldn't reach you after 4 attempts. No more calls are scheduled."
            assert manager.report(row['id'], attempt['id'], attempt['execution_id'])['report'] == saved['report']
            assert manager.report(row['id'], attempt['id'], attempt['execution_id'])['report'] == ''
    assert row['automatic_retries_used'] == 3
    assert len(manager.jobs.jobs) == 4


@pytest.mark.parametrize('uncertain,active', [(True, False), (False, True)])
def test_unknown_or_active_never_automatically_retried(manager, uncertain, active):
    row = schedule(manager)
    row, attempt = admit(manager, row['id'])
    observed(manager, row, attempt, uncertain=uncertain, active=active)
    assert manager.routine_decision(row['id'], attempt['id'])
    current = manager.store.get(row['id'])
    assert current['status'] == 'uncertain' and len(manager.jobs.jobs) == 1
    assert current['automatic_retries_used'] == 0


def test_empty_policy_disables_retries(manager):
    manager.bridge.config = replace(manager.bridge.config, callback_retry_delays=())
    row = schedule(manager)
    row, attempt = admit(manager, row['id'])
    observed(manager, row, attempt)
    manager.routine_decision(row['id'], attempt['id'])
    assert manager.store.get(row['id'])['status'] == 'exhausted'
    assert len(manager.jobs.jobs) == 1


@pytest.mark.parametrize('connected,reason,status', [(True, 'disconnected', 'completed'),
    (False, 'command_rejected', 'needs_guidance')])
def test_reminders_finish_without_evaluator_or_claiming_hearing(manager, connected, reason, status):
    row = manager.schedule({'request_id': 'simple', 'purpose': 'Remind me to start recording a video.',
        'delay_minutes': 3}, 'hfp-admin', kind='reminder')['callback']
    row, attempt = admit(manager, row['id'])
    observed(manager, row, attempt, reason=reason, connected=connected)
    assert manager.routine_decision(row['id'], attempt['id'])
    manager.finish_report(row['id'], attempt['id'])
    result = manager.store.get(row['id'])
    assert result['status'] == status and len(manager.jobs.jobs) == 1
    if connected:
        assert result['delivery_status'] == 'call_finished_not_confirmed_heard'
        assert result['attempts'][-1]['report'] == ''


def callback_body(attempt, request_id='later', **timing):
    return {'attempt_id': attempt['id'], 'execution_id': attempt['execution_id'], 'call_id': 'physical',
        'request_id': request_id, 'time': timing or {'delay_minutes': 20}}


def test_recipient_callback_preserves_objective_budget_and_replaces_pending(manager, monkeypatch):
    row = schedule(manager)
    row, attempt = admit(manager, row['id'])
    monkeypatch.setattr('hfp_mcp.phone_cli.control', lambda *a: {'active': True, 'finished': False, 'call_id': 'physical'})
    confirmations = []
    monkeypatch.setattr(manager, 'callback_confirmation', lambda *a: confirmations.append(a))
    body = callback_body(attempt)
    first = manager.request_callback(row['id'], body)
    assert manager.request_callback(row['id'], body) == first
    assert len(confirmations) == 1
    child = first['callback']
    assert child['root_request_id'] == row['id'] and child['purpose'] == row['purpose'] and child['number'] == row['number']
    assert child['deadline'] == row['deadline'] and child['max_attempts'] == 4
    assert child['automatic_retries_used'] == 0
    revised = manager.request_callback(row['id'], callback_body(attempt, 'later-revised', delay_minutes=30))
    assert revised['callback']['id'] == child['id']
    assert revised['callback']['run_at'] > child['run_at'] and len(manager.jobs.jobs) == 2
    observed(manager, row, attempt, connected=True, reason='disconnected')
    manager.routine_decision(row['id'], attempt['id'])
    assert manager.store.get(row['id'])['status'] == 'completed'
    # The first missed continuation uses the chain's first delay, not a reset limit.
    child, next_attempt = admit(manager, child['id'])
    observed(manager, child, next_attempt)
    manager.routine_decision(child['id'], next_attempt['id'])
    root = manager.store.get(row['id'])
    assert root['automatic_retries_used'] == 1
    assert manager.cancel(child['id'])['ok']
    assert all(r['status'] == 'cancelled' for r in manager.store.rows())


@pytest.mark.parametrize('change', ['wrong_call', 'ended', 'foreign_attempt', 'purpose', 'ambiguous', 'no_timezone'])
def test_recipient_callback_is_narrow_and_requires_active_call(manager, monkeypatch, change):
    row = schedule(manager)
    row, attempt = admit(manager, row['id'])
    state = {'active': True, 'finished': False, 'call_id': 'physical'}
    monkeypatch.setattr('hfp_mcp.phone_cli.control', lambda *a: state)
    body = callback_body(attempt)
    if change == 'wrong_call': body['call_id'] = 'other'
    if change == 'ended': state['finished'] = True
    if change == 'foreign_attempt': body['attempt_id'] = 'another'
    if change == 'purpose': body['time']['purpose'] = 'call another person'
    if change == 'ambiguous': body['time'] = {}
    if change == 'no_timezone': body['time'] = {'run_at': '2026-10-19T10:00:00'}
    with pytest.raises((ValueError, PermissionError, StopIteration)):
        manager.request_callback(row['id'], body)
    assert len(manager.jobs.jobs) == 1


def test_lost_callback_schedule_recovers_without_duplicate_or_false_confirmation(manager, monkeypatch):
    row = schedule(manager)
    row, attempt = admit(manager, row['id'])
    monkeypatch.setattr('hfp_mcp.phone_cli.control', lambda *a: {'active': True, 'finished': False, 'call_id': 'physical'})
    manager.jobs.fail_create = True
    with pytest.raises(TimeoutError):
        manager.request_callback(row['id'], callback_body(attempt))
    manager.jobs.fail_create = False
    manager.reconcile_schedules()
    child_id = manager.store.get(row['id'])['attempts'][0]['requested_callback_id']
    assert manager.store.get(child_id)['status'] == 'scheduled'
    assert len(manager.jobs.jobs) == 2
    assert manager.request_callback(row['id'], callback_body(attempt))['callback']['id'] == child_id
    assert len(manager.jobs.jobs) == 2


def test_connected_general_deferral_does_not_consume_retry_budget(manager):
    row, attempt = deciding(manager, connected=True)
    with manager.store.lock, manager.store.db:
        row['automatic_retries_used'] = 3
        manager.store.save(row)
    result = decide(manager, row, attempt, disposition='deferred', evidence_ids=['transcript:1'],
        run_at=datetime.fromtimestamp(time.time()+1200, timezone.utc).isoformat())
    assert result['ok'] and result['callback']['automatic_retries_used'] == 3


@pytest.mark.parametrize('mode,method', [('gateway', 'by message'), ('call', 'by phone'), ('both', 'by message and phone')])
def test_schedule_message_is_readable(mode, method):
    message = schedule_confirmation([{'verified': True, 'callback_run_at': 1792324200,
        'timezone': 'Asia/Kolkata', 'purpose': 'Remind me to start recording a video.', 'delivery': mode,
        'run_id': 'secret', 'destination': {'chat_id': 'secret'}}])
    assert 'to start recording a video' in message and 'IST' in message and method in message
    assert 'secret' not in message and '1792324200' not in message and '[completed]' not in message


def test_outgoing_brief_wins_over_retained_incoming_dialogue_and_reconnect():
    config = _live_config('Old call: How can I help you? Wait for a new request.', None, {'request_callback'},
        call_direction='outgoing', call_purpose='Remind me to start recording a video.', cached_spoken_opening=True,
        continuing_call=True)['system_instruction']
    assert 'Active purpose for THIS outgoing call' in config and 'start recording a video' in config
    assert 'FIRST substantive reply must immediately explain' in config
    assert 'SAME physical call' in config and 'Ask what time' in config
    declaration = _function_declarations({'request_callback'})[0]
    assert set(declaration['parameters']['properties']) == {'delay_minutes', 'delay_seconds', 'run_at'}


@pytest.mark.parametrize('mismatch', [None, 'generation', 'incoming', 'number'])
async def test_restricted_voice_callback_uses_trusted_physical_call(tmp_path, mismatch):
    from hfp_mcp.contracts import RequestLedger
    from tests.test_outbound_brief import controller
    c = controller()
    c.route = replace(c.route, policy='guest')
    c.ledger = RequestLedger(tmp_path / 'calls.db')
    c.outbound_context = {'call_id': 'call', 'purpose': 'Remind me to start recording a video.', 'request_id': 'attempt'}
    record = {'call_id': 'call', 'number': c._number, 'call_generation': 4,
        'envelope': {'callback_id': 'trusted-request', 'attempt_id': 'attempt', 'execution_id': 'native-execution'}}
    if mismatch == 'generation': record['call_generation'] = 3
    if mismatch == 'incoming': c.snapshot()['call']['direction'] = 'incoming'
    if mismatch == 'number': record['number'] = '+919876543299'
    c.ledger._db.execute('CREATE TABLE phone_workflow_execution (id TEXT PRIMARY KEY, data TEXT)')
    c.ledger._db.execute('INSERT INTO phone_workflow_execution VALUES (?,?)', ('attempt', json.dumps(record)))
    c.ledger._db.commit()
    calls = []
    class API:
        async def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'status': 'ok', 'message': 'Saved verified callback.'}
    c.api = API()
    try:
        result = await c.delegate(SimpleNamespace(name='request_callback', request_id='voice-later', arguments={'delay_minutes': 20}))
        if mismatch:
            assert result['status'] == 'denied' and not calls
        else:
            assert result['status'] == 'ok'
            assert calls[0][0][1] == 'v1/hfp/workflows/trusted-request/callback'
            assert calls[0][1]['json']['call_id'] == 'call'
            assert set(calls[0][1]['json']['time']) == {'delay_minutes'}
            # This narrow grant does not authorize general owner actions.
            assert not c.config.policies[c.route.policy].admin
    finally:
        c.ledger.close()


@pytest.mark.parametrize('mode', ['call', 'both'])
def test_script_errors_are_private_and_call_only_is_silent(manager, monkeypatch, capsys, mode):
    from hfp_mcp import phone_workflow_runner as runner
    from hfp_mcp.phone_workflows import WorkflowStore
    row = manager.schedule({'request_id': 'script-error', 'purpose': 'Start recording a video.', 'delay_minutes': 3},
        'hfp-admin', delivery_plan={'mode': mode, 'gateway': None, 'notify_at_due': False}, kind='reminder')['callback']
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: manager.store.path.parent.parent))
    async def failure():
        raise RuntimeError('technical transport detail')
    runner.run_script(failure(), row['id'], 'owner')
    text = capsys.readouterr().out
    assert 'RuntimeError' not in text and 'technical transport detail' not in text
    assert (text == '') if mode == 'call' else "couldn't confirm" in text
    diagnostic = manager.store.get(row['id'])['last_script_error']
    assert diagnostic['type'] == 'RuntimeError' and diagnostic['detail'] == 'technical transport detail'


@pytest.mark.parametrize('connected', [False, True])
async def test_routine_run_never_starts_evaluator(manager, monkeypatch, connected):
    from hfp_mcp import hermes_api, phone_cli
    row = manager.schedule({'request_id': 'routine', 'purpose': 'Start recording a video.', 'delay_minutes': 3},
        'hfp-admin', kind='reminder')['callback']
    row, attempt = admit(manager, row['id'])
    outcome = {'connection_state': 'connected' if connected else 'not_connected',
        'termination_reason': 'disconnected' if connected else 'busy', 'ended_at': time.time(),
        'uncertain': False, 'still_active': False, 'evidence': [], 'verified_actions': []}
    monkeypatch.setattr(phone_cli, 'control', lambda path, body=None: {'ok': True} if path.endswith('workflow-call')
        else {'finished': True, 'outcome': outcome})
    def forbidden(*a, **kw):
        pytest.fail('Routine reminder and missed-call outcomes must not create another AI run')
    monkeypatch.setattr(hermes_api, 'HermesAPI', forbidden)
    await manager.run(row['id'], attempt['id'])
    saved = manager.store.get(row['id'])
    assert saved['status'] == ('completed' if connected else 'scheduled')
    assert saved['attempts'][-1]['decision']['source'] == 'programmed_policy'
    assert saved['attempts'][-1]['report'] == ''
