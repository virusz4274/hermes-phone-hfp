import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from hfp_mcp.phone_workflows import GatewayPhoneWorkflows, WorkflowConflict, WorkflowStore, retry_time
from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


class FakeJobs:
    def __init__(self, log=None):
        self.jobs, self.executions = {}, {}
        self.log = log if log is not None else []
        self.fail_create = self.fail_move = self.fail_arm = False

    @staticmethod
    def instant(job):
        return datetime.fromisoformat(job['schedule']['run_at']).timestamp()

    def create(self, row, ordinal):
        from hfp_mcp.phone_workflows import NativePhoneJobs
        destination = NativePhoneJobs.destination(row)
        key = row['id'] + ':' + str(ordinal)
        if key not in self.jobs:
            import hashlib
            script = 'hfp-phone-' + hashlib.sha256(row['id'].encode()).hexdigest() + '-' + str(ordinal) + '.py'
            job = {'id': key, 'state': 'paused', 'script': script, 'no_agent': True, 'last_run_at': None,
                   'schedule': {'run_at': datetime.fromtimestamp(row['run_at'], timezone.utc).isoformat()}}
            self.jobs[key] = job
            self.log.append({'no_agent': True, 'script': script, 'schedule': job['schedule']['run_at'], 'deliver': destination})
            if self.fail_create:
                raise TimeoutError('native response lost after durable creation')
        return self.jobs[key]

    def create_notification(self, row):
        import hashlib
        from hfp_mcp.phone_workflows import NativePhoneJobs
        key = row['id'] + ':notification'
        if key not in self.jobs:
            job = {'id': key, 'state': 'paused', 'last_run_at': None, 'no_agent': True,
                   'script': 'hfp-reminder-' + hashlib.sha256(row['id'].encode()).hexdigest() + '.py',
                   'schedule': {'run_at': datetime.fromtimestamp(row['notification_run_at'], timezone.utc).isoformat()}}
            self.jobs[key] = job
            self.log.append({'deliver': NativePhoneJobs.report_destination(row), 'no_agent': True,
                             'script': job['script'], 'schedule': job['schedule']['run_at']})
        return self.jobs[key]

    def get(self, key):
        return self.jobs.get(key)

    def arm(self, key):
        if self.fail_arm:
            raise TimeoutError('arm response missing')
        self.jobs[key]['state'] = 'scheduled'
        return self.jobs[key]

    def pause(self, key):
        self.jobs[key]['state'] = 'paused'
        return self.jobs[key]

    def move(self, key, when):
        self.jobs[key]['schedule']['run_at'] = datetime.fromtimestamp(when, timezone.utc).isoformat()
        if self.fail_move:
            raise TimeoutError('move response lost')
        return self.jobs[key]

    def fenced(self, key, fn):
        return fn()

    def execution(self, job_id, execution_id):
        execution = self.executions[execution_id]
        if execution['job_id'] != job_id or execution['status'] != 'running':
            raise PermissionError('not a running native execution')
        return self.jobs[job_id], execution

    def fire(self, row, execution_id='execution'):
        self.executions[execution_id] = {'job_id': row['cron_job_id'], 'status': 'running',
                                        'scheduled_instant': self.jobs[row['cron_job_id']]['schedule']['run_at']}
        return execution_id


@pytest.fixture
def manager(tmp_path, monkeypatch):
    cfg = RoutingConfig.parse(routing_data())
    monkeypatch.setattr(RoutingConfig, 'load', classmethod(lambda cls: cfg))
    monkeypatch.setattr('hfp_mcp.reminders.telegram_destination', lambda: 'telegram')
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', lambda: {
        'platform': 'telegram', 'chat_id': 'owner', 'thread_id': None, 'deliver': 'telegram:owner'})
    monkeypatch.setattr('hfp_mcp.hermes_compat.validate_gateway_destination', lambda _: None)
    store = CallerStore(tmp_path / 'hfp-phone/callers.sqlite3')
    bridge = PhoneBridge(cfg, store, profile='default', profile_config={})
    bridge.workflows.jobs = FakeJobs()
    store.bind(session_id='hfp-admin', call_id='call', profile='default', number='+919876543210', policy={'admin': True}, ttl=60)
    store.bind(session_id='hfp-guest', call_id='guest', profile='default', number='+919876543211', policy={'admin': False, 'tools': []}, ttl=60)
    yield bridge.workflows
    bridge.workflows.store.close()
    store.close()


def schedule(manager, key='stable'):
    return manager.schedule({'request_id': key, 'purpose': 'Remind the recipient about uploading the video and ask if they can do it.',
                             'run_at': datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()}, 'hfp-admin')['callback']


def deciding(manager, *, connected=False, uncertain=False, count=1):
    row = schedule(manager)
    for ordinal in range(count):
        if ordinal:
            with manager.store.lock, manager.store.db:
                row = manager.store.get(row['id'])
                row['status'] = 'holding'
                manager.store.save(row)
            manager.publish(row)
        row = manager.store.get(row['id'])
        execution = manager.jobs.fire(row, 'execution' + str(ordinal))
        row, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
        row, attempt = manager.store.update_attempt(row['id'], attempt['id'], status='deciding',
            decision_deadline=time.time() + 120, decision_run_id='run',
            outcome={'connection_state': 'connected' if connected else 'not_connected', 'uncertain': uncertain,
                     'still_active': False, 'termination_reason': 'busy', 'evidence': [{'id': 'transcript:1', 'kind': 'transcript', 'direction': 'input'}], 'verified_actions': []})
    return row, attempt


def decide(manager, row, attempt, **changes):
    args = {'callback_id': row['id'], 'attempt_id': attempt['id'], 'request_id': 'decision-' + attempt['id'],
            'expected_revision': row['revision'], 'action': 'retry', 'disposition': 'incomplete', 'reason': 'Recipient did not answer.'}
    args.update(changes)
    return manager.apply(args, attempt['decision_session'], 'run')


def test_creation_native_only_and_exact_schedule(manager):
    row = schedule(manager)
    assert row['scheduler'] == 'hermes' and row['reminder_job_id'] == row['cron_job_id']
    assert len(manager.jobs.jobs) == 1 and manager.jobs.log[0]['no_agent']
    assert row['verification']['callback_run_at'] == row['verification']['reminder_run_at']
    assert row['status'] == 'scheduled' and row['attempt_count'] == 0


def reminder(manager, mode='both', key='reminder'):
    return manager.schedule({'request_id': key, 'purpose': 'Remember to cut your hair',
        'run_at': datetime.fromtimestamp(time.time()+600, timezone.utc).isoformat()}, 'hfp-admin',
        delivery_plan={'mode': mode, 'gateway': None if mode == 'call' else {
            'platform': 'slack', 'chat_id': 'home', 'thread_id': '42', 'deliver': 'slack:home:42'},
            'notify_at_due': mode == 'both'})['callback']


def fire_notification(manager, row, execution_id='notice'):
    job_id = row['notification_job_id']
    manager.jobs.executions[execution_id] = {'job_id': job_id, 'status': 'running',
        'scheduled_instant': manager.jobs.get(job_id)['schedule']['run_at']}
    return manager.notification(row['id'], job_id, execution_id)


def test_both_reminder_moves_and_cancels_both_components(manager):
    row = reminder(manager)
    assert len(manager.jobs.jobs) == 2
    assert manager.jobs.log[0]['deliver'] == 'local'
    assert manager.jobs.log[1]['deliver'] == 'slack:home:42'
    moved = manager.reschedule({'callback_id': row['id'], 'request_id': 'move-both',
        'expected_revision': row['revision'],
        'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})['callback']
    assert moved['run_at'] == moved['notification_run_at']
    assert manager.jobs.instant(manager.jobs.get(row['cron_job_id'])) == manager.jobs.instant(manager.jobs.get(row['notification_job_id']))
    cancelled = manager.cancel(row['id'])
    assert cancelled['ok'] and all(j['state'] == 'paused' for j in manager.jobs.jobs.values())
    with pytest.raises(PermissionError):
        fire_notification(manager, moved)


def test_notification_claim_is_durable_and_does_not_repeat_on_call_retry(manager):
    row = reminder(manager)
    first = fire_notification(manager, row)
    assert first['report'] == 'Reminder: Remember to cut your hair.'
    assert fire_notification(manager, row)['report'] == ''
    assert manager.store.get(row['id'])['notification_claimed']
    # A new callback attempt must reuse the already-created initial reminder.
    with manager.store.lock, manager.store.db:
        current = manager.store.get(row['id'])
        current.update(status='holding', attempts=[{'id': 'prior', 'ordinal': 1}])
        manager.store.save(current)
    manager.publish(current)
    assert len([j for j in manager.jobs.jobs if j.endswith(':notification')]) == 1


def test_failed_gateway_does_not_block_callback_and_is_visible(manager, monkeypatch):
    row = reminder(manager)
    def unavailable(_):
        raise ValueError('Gateway disconnected')
    monkeypatch.setattr('hfp_mcp.hermes_compat.validate_gateway_destination', unavailable)
    with pytest.raises(ValueError, match='disconnected'):
        fire_notification(manager, row)
    manager.jobs.get(row['notification_job_id']).update(state='error', last_error='Gateway disconnected')
    execution = manager.jobs.fire(row)
    assert manager.admit(row['id'], row['cron_job_id'], execution)[2]
    status = manager.public(manager.store.get(row['id']))
    assert status['components']['gateway_reminder']['last_error'] == 'Gateway disconnected'


def test_partial_linked_move_recovers_without_new_jobs(manager, monkeypatch):
    row = reminder(manager)
    move = manager.jobs.move
    def fail_second(job_id, instant):
        if job_id == row['notification_job_id']:
            raise TimeoutError('Reminder move response lost')
        return move(job_id, instant)
    monkeypatch.setattr(manager.jobs, 'move', fail_second)
    args = {'callback_id': row['id'], 'request_id': 'partial-both', 'expected_revision': row['revision'],
            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()}
    with pytest.raises(TimeoutError):
        manager.reschedule(args)
    assert manager.store.get(row['id'])['status'] == 'holding'
    with pytest.raises(PermissionError):
        fire_notification(manager, row)
    monkeypatch.setattr(manager.jobs, 'move', move)
    manager.reconcile_schedules()
    moved = manager.reschedule(args)['callback']
    assert moved['run_at'] == moved['notification_run_at'] and len(manager.jobs.jobs) == 2


def test_missing_linked_notification_cannot_be_silently_rescheduled(manager):
    row = reminder(manager)
    manager.jobs.jobs.pop(row['notification_job_id'])
    with pytest.raises(WorkflowConflict):
        manager.reschedule({'callback_id': row['id'], 'request_id': 'missing-notice', 'expected_revision': 0,
            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})


def test_call_only_outcome_stays_local(manager):
    row = reminder(manager, 'call')
    execution = manager.jobs.fire(row)
    _, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
    manager.store.update_attempt(row['id'], attempt['id'], report='Completed reminder', status='decided')
    response = manager.report(row['id'], attempt['id'], execution)
    assert response['ready'] and response['report'] == '' and response['delivery'] == 'local'
    assert len(manager.jobs.jobs) == 1


def test_lost_linked_creation_response_recovers_the_existing_jobs(manager, monkeypatch):
    create = manager.jobs.create_notification
    def lost(row):
        create(row)
        raise TimeoutError('Response lost after durable reminder creation')
    monkeypatch.setattr(manager.jobs, 'create_notification', lost)
    with pytest.raises(TimeoutError):
        reminder(manager)
    held = manager.store.rows()[0]
    assert held['status'] == 'holding' and len(manager.jobs.jobs) == 2
    assert held['component_errors']['scheduling']
    monkeypatch.setattr(manager.jobs, 'create_notification', create)
    manager.reconcile_schedules()
    recovered = manager.store.get(held['id'])
    assert recovered['status'] == 'scheduled' and recovered['notification_job_id']
    assert len(manager.jobs.jobs) == 2 and not recovered['component_errors']


def test_both_outcome_uses_a_separate_report_job(manager):
    row = reminder(manager)
    execution = manager.jobs.fire(row)
    _, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
    manager.store.update_attempt(row['id'], attempt['id'], report='Completed reminder', status='decided')
    response = manager.report(row['id'], attempt['id'], execution)
    assert response['ready'] and response['report'] == ''
    assert response['delivery'] == 'separate_report_job'
    assert not manager.store.get(row['id'])['attempts'][0]['report_claimed']


def test_legacy_workflow_keeps_telegram_policy_without_native_default_resolution(manager, monkeypatch):
    def unavailable():
        raise AssertionError('Legacy workflow must not select a new gateway')
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', unavailable)
    row = manager.store.create(request_id='legacy', number='+919876543210', purpose='Existing callback',
        run_at=time.time()+600, authority=manager.authority('hfp-admin'), timezone_name='UTC')
    assert manager.publish(row)['ok']
    assert manager.jobs.log[0]['deliver'] == 'telegram'
    assert 'delivery_plan' not in manager.store.get(row['id'])


def test_duplicate_creation_survives_restart_and_lost_response(manager):
    args = {'request_id': 'lost', 'purpose': 'Call me.', 'run_at': datetime.fromtimestamp(time.time()+600, timezone.utc).isoformat()}
    manager.jobs.fail_create = True
    with pytest.raises(TimeoutError):
        manager.schedule(args, 'hfp-admin')
    path = manager.store.path
    manager.store.close()
    manager.store = WorkflowStore(path, 'default')
    manager.jobs.fail_create = False
    result = manager.schedule(args, 'hfp-admin')
    assert result['ok'] and len(manager.jobs.jobs) == 1
    assert manager.schedule(args, 'hfp-admin')['callback']['id'] == result['callback']['id']
    with pytest.raises(WorkflowConflict):
        manager.schedule({**args, 'purpose': 'Different request'}, 'hfp-admin')


@pytest.mark.parametrize('sid', ['hfp-guest', 'hfp-missing'])
def test_guest_and_expired_authority_denied(manager, sid):
    with pytest.raises(PermissionError):
        manager.schedule({'purpose': 'Call me', 'delay_minutes': 2}, sid)
    assert not manager.jobs.jobs


def test_scheduling_grant_does_not_revive_call_binding(manager):
    row = schedule(manager)
    manager.bridge.store.revoke('hfp-admin')
    manager.authorize(manager.store.get(row['id']))
    with pytest.raises(PermissionError):
        manager.bridge.store.binding('hfp-admin')
    manager.bridge.config.numbers.clear()
    with pytest.raises(PermissionError):
        manager.authorize(manager.store.get(row['id']))


def test_reschedule_preserves_id_and_old_occurrence_cannot_fire(manager):
    row = schedule(manager)
    original = manager.jobs.fire(row, 'old')
    moved = manager.reschedule({'callback_id': row['id'], 'request_id': 'move', 'expected_revision': row['revision'],
                                'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})['callback']
    assert row['cron_job_id'] == moved['cron_job_id'] and moved['revision'] == row['revision']+1
    with pytest.raises(PermissionError, match='occurrence'):
        manager.admit(row['id'], row['cron_job_id'], original)
    with pytest.raises(WorkflowConflict):
        manager.reschedule({'callback_id': row['id'], 'request_id': 'stale', 'expected_revision': row['revision'],
                            'run_at': datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat()})


def test_cancel_during_native_move_cannot_be_overwritten(manager, monkeypatch):
    row = schedule(manager)
    move = manager.jobs.move
    def racing_move(job_id, target):
        result = move(job_id, target)
        manager.cancel(row['id'])
        return result
    monkeypatch.setattr(manager.jobs, 'move', racing_move)
    with pytest.raises(WorkflowConflict, match='changed during'):
        manager.reschedule({'callback_id': row['id'], 'request_id': 'cancel-race', 'expected_revision': row['revision'],
                            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})
    current = manager.store.get(row['id'])
    assert current['status'] == 'cancelled'
    manager.jobs.fire(current)
    with pytest.raises(PermissionError):
        manager.admit(row['id'], row['cron_job_id'], 'execution')


@pytest.mark.parametrize('hour,allowed', [(7, False), (8, True), (21, True), (22, False)])
def test_native_retry_catchup_checks_actual_calling_hours(manager, monkeypatch, hour, allowed):
    from zoneinfo import ZoneInfo
    row, attempt = deciding(manager)
    decide(manager, row, attempt)
    row = manager.store.get(row['id'])
    assert row['automatic_hours']
    actual = datetime.fromtimestamp(row['run_at'], ZoneInfo(row['timezone'])).replace(hour=hour)
    monkeypatch.setattr('hfp_mcp.phone_workflows.time.time', lambda: actual.timestamp())
    execution = manager.jobs.fire(row, 'retry-catchup')
    if allowed:
        assert manager.admit(row['id'], row['cron_job_id'], execution)[2]
    else:
        with pytest.raises(PermissionError, match='outside calling hours'):
            manager.admit(row['id'], row['cron_job_id'], execution)
        current = manager.store.get(row['id'])
        assert current['status'] == 'needs_guidance' and len(current['attempts']) == 1


def test_partial_move_fences_execution_across_restart(manager):
    row = schedule(manager)
    manager.jobs.fail_move = True
    with pytest.raises(TimeoutError):
        manager.reschedule({'callback_id': row['id'], 'request_id': 'move', 'expected_revision': 0,
                            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})
    assert manager.store.get(row['id'])['status'] == 'holding'
    manager.jobs.fire(manager.store.get(row['id']))
    with pytest.raises((WorkflowConflict, PermissionError)):
        manager.admit(row['id'], row['cron_job_id'], 'execution')


def test_native_edit_detected_before_execution(manager):
    row = schedule(manager)
    manager.jobs.move(row['cron_job_id'], row['run_at']+60)
    manager.jobs.fire(row)
    with pytest.raises((WorkflowConflict, PermissionError)):
        manager.admit(row['id'], row['cron_job_id'], 'execution')


def test_duplicate_execution_never_launches_second_attempt(manager):
    row = schedule(manager)
    execution = manager.jobs.fire(row)
    first, attempt, created = manager.admit(row['id'], row['cron_job_id'], execution)
    second, replay, created_again = manager.admit(row['id'], row['cron_job_id'], execution)
    assert created and not created_again and attempt['id'] == replay['id']
    assert len(second['attempts']) == 1 and first['status'] == 'firing'
    with pytest.raises(WorkflowConflict):
        manager.reschedule({'callback_id': row['id'], 'request_id': 'move', 'expected_revision': first['revision'],
                            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()})


def test_connection_does_not_complete_objective(manager):
    row, attempt = deciding(manager, connected=True)
    assert row['status'] == 'firing'
    with pytest.raises(ValueError, match='evidence'):
        decide(manager, row, attempt, action='close', disposition='completed')


@pytest.mark.parametrize('disposition', ['completed', 'declined'])
def test_evidenced_completion_or_refusal_closes_without_retry(manager, disposition):
    row, attempt = deciding(manager, connected=True)
    result = decide(manager, row, attempt, action='close', disposition=disposition, evidence_ids=['transcript:1'])
    assert result['callback']['status'] == disposition
    assert len(manager.jobs.jobs) == 1
    assert decide(manager, row, attempt, action='close', disposition=disposition, evidence_ids=['transcript:1']) == result


def test_retry_single_native_job_and_receipt_verified(manager):
    row, attempt = deciding(manager)
    result = decide(manager, row, attempt)
    assert result['ok'] and result['verification']['verified']
    assert result['callback']['attempt_count'] == 1 and len(manager.jobs.jobs) == 2
    assert len([j for j in manager.jobs.jobs.values() if j['state']=='scheduled' and not j.get('last_run_at')]) >= 1


def test_uncertain_outcome_cannot_retry(manager):
    row, attempt = deciding(manager, uncertain=True)
    with pytest.raises(ValueError, match='cannot be retried'):
        decide(manager, row, attempt)


def test_fourth_attempt_requires_owner_guidance(manager):
    row, attempt = deciding(manager, count=4)
    with manager.store.lock, manager.store.db:
        row['automatic_retries_used'] = 3
        manager.store.save(row)
    with pytest.raises(ValueError, match='attempt limit'):
        decide(manager, row, attempt)


def test_recipient_deferral_requires_specific_evidenced_time(manager):
    row, attempt = deciding(manager, connected=True)
    with pytest.raises(ValueError, match='specific time'):
        decide(manager, row, attempt, disposition='deferred', evidence_ids=['transcript:1'])


def test_cancel_active_stops_late_decision_without_hangup(manager):
    row, attempt = deciding(manager, connected=True)
    assert manager.cancel(row['id'])['active_call_ended'] is False
    with pytest.raises(PermissionError):
        decide(manager, row, attempt)
    assert manager.cancel(row['id'])['ok']


def test_wrong_call_and_expired_followup_run_denied(manager):
    row, attempt = deciding(manager)
    with pytest.raises(PermissionError):
        manager.decision_authority(attempt['decision_session'], 'another-run')
    manager.store.update_attempt(row['id'], attempt['id'], decision_deadline=time.time()-1)
    with pytest.raises(PermissionError):
        decide(manager, row, attempt)


def test_calling_hours_and_deadline():
    zone = timezone.utc
    now = datetime(2026, 10, 8, 21, 59, tzinfo=zone).timestamp()
    row = {'timezone': 'UTC', 'attempts': [1], 'deadline': now + 7*86400}
    actual = datetime.fromtimestamp(retry_time(row, now=now), zone)
    assert (actual.day, actual.hour, actual.minute) == (9,8,0)
    with pytest.raises(ValueError, match='two minutes'):
        retry_time(row, now+30, now=now)
    with pytest.raises(ValueError, match='deadline'):
        retry_time({**row, 'deadline': now+60}, now=now)


async def test_connected_objective_drives_hermes_decision_without_routine_report(manager, monkeypatch):
    from hfp_mcp import hermes_api, hermes_sessions, phone_cli
    row = schedule(manager)
    execution = manager.jobs.fire(row)
    row, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
    calls = []
    outcome = {'connection_state': 'connected', 'termination_reason': 'disconnected', 'uncertain': False,
               'still_active': False, 'evidence': [], 'verified_actions': []}
    def control(path, body=None):
        calls.append((path, body))
        return {'ok': True} if path.endswith('workflow-call') else {'finished': True, 'outcome': outcome}
    monkeypatch.setattr(phone_cli, 'control', control)
    monkeypatch.setattr(hermes_sessions, 'create', lambda *a: a[1])
    class API:
        def __init__(self, *a): pass
        async def request(self, method, path, **kw):
            assert path == 'v1/runs' and method == 'POST'
            assert 'original_request' in kw['json']['input'] and 'attempt_history' in kw['json']['input']
            manager.store.update_attempt(row['id'], attempt['id'], decision_run_id='run')
            current = manager.store.get(row['id'])
            active = current['attempts'][-1]
            decide(manager, current, active)
            return {'run_id': 'run'}
        async def close(self): pass
        async def stop(self, *a): pass
    monkeypatch.setattr(hermes_api, 'HermesAPI', API)
    await manager.run(row['id'], attempt['id'])
    assert sum(path.endswith('workflow-call') for path, _ in calls) == 1
    report = manager.report(row['id'], attempt['id'], execution)
    assert report['ready'] and report['report'] == ''
    assert 'Next call:' in manager.store.get(row['id'])['attempts'][0]['diagnostic_report']
    assert manager.report(row['id'], attempt['id'], execution)['report'] == ''
    assert len(manager.store.get(row['id'])['attempts']) == 1


async def test_restart_missing_dial_receipt_never_redials(manager, monkeypatch):
    from hfp_mcp import phone_cli
    row = schedule(manager)
    execution = manager.jobs.fire(row)
    row, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
    calls = []
    def control(path, body=None):
        calls.append(path)
        return {'missing': True, 'finished': False}
    monkeypatch.setattr(phone_cli, 'control', control)
    await manager.run(row['id'], attempt['id'], recover=True)
    assert not any(p.endswith('workflow-call') for p in calls)
    current = manager.store.get(row['id'])
    assert current['status'] == 'uncertain' and current['attempts'][0]['outcome']['uncertain']


def test_restart_reconciles_verified_move_without_replaying_call(manager):
    row = schedule(manager)
    manager.jobs.fail_move = True
    args = {'callback_id': row['id'], 'request_id': 'move', 'expected_revision': 0,
            'run_at': datetime.fromtimestamp(time.time()+1800, timezone.utc).isoformat()}
    with pytest.raises(TimeoutError): manager.reschedule(args)
    manager.jobs.fail_move = False
    manager.reconcile_schedules()
    assert manager.store.get(row['id'])['status'] == 'scheduled'
    assert manager.reschedule(args)['verification']['verified']
    assert not manager.store.get(row['id'])['attempts']


def test_report_is_rechecked_after_owner_revocation(manager):
    row, attempt = deciding(manager, connected=True)
    decide(manager, row, attempt, action='close', disposition='completed', evidence_ids=['transcript:1'])
    manager.finish_report(row['id'], attempt['id'])
    manager.bridge.config.numbers.clear()
    with pytest.raises(PermissionError):
        manager.report(row['id'], attempt['id'], attempt['execution_id'])


def test_other_profile_cannot_read_request(manager):
    other = WorkflowStore(manager.store.path, 'other')
    row = schedule(manager)
    try:
        with pytest.raises(ValueError): other.get(row['id'])
        assert not other.rows()
    finally:
        other.close()


def test_lost_native_firing_ownership_cannot_dial_or_report(manager):
    row = schedule(manager)
    manager.jobs.get(row['cron_job_id'])['fire_claim'] = {'by': 'original'}
    execution = manager.jobs.fire(row)
    row, attempt, _ = manager.admit(row['id'], row['cron_job_id'], execution)
    manager.jobs.get(row['cron_job_id'])['fire_claim'] = {'by': 'replacement'}
    with pytest.raises(PermissionError):
        manager.daemon_authority(row['id'], attempt['id'], execution)
    manager.finish_report(row['id'], attempt['id'])
    with pytest.raises(PermissionError):
        manager.report(row['id'], attempt['id'], execution)


def test_valid_recipient_deferral_schedules_exact_time(manager):
    row, attempt = deciding(manager, connected=True)
    when = datetime.fromtimestamp(time.time()+86400, timezone.utc).isoformat()
    result = decide(manager, row, attempt, disposition='deferred', evidence_ids=['transcript:1'], run_at=when)
    assert result['ok'] and result['callback']['run_at'] == datetime.fromisoformat(when).timestamp()
    assert result['callback']['attempt_count'] == 1


def test_third_attempt_can_request_owner_guidance(manager):
    row, attempt = deciding(manager, count=3)
    result = decide(manager, row, attempt, action='ask_owner', disposition='incomplete')
    assert result['callback']['status'] == 'needs_guidance' and len(manager.jobs.jobs) == 3


def test_retry_brief_anchors_relative_dates_and_preserves_original_goal(manager):
    row, attempt = deciding(manager, connected=True)
    row['purpose'] = 'Ask the recipient to upload today.'
    row['created_at'] = datetime(2026,10,8,10,tzinfo=timezone.utc).timestamp()
    brief = manager.call_brief(row)
    assert '2026-10-08' in brief and 'original date, not this retry date' in brief
    assert row['purpose'] in brief and 'Prior verified progress' in brief


def test_assistant_statement_alone_cannot_complete_objective(manager):
    row, attempt = deciding(manager, connected=True)
    manager.store.update_attempt(row['id'], attempt['id'], outcome={**attempt['outcome'],
        'evidence':[{'id':'transcript:1','kind':'transcript','direction':'output','text':'I have completed it.'}]})
    with pytest.raises(ValueError, match='evidence'):
        decide(manager, row, attempt, action='close', disposition='completed', evidence_ids=['transcript:1'])
