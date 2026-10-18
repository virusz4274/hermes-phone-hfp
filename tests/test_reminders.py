import json
import sys
import time
from types import SimpleNamespace
from datetime import datetime

import pytest

from hfp_mcp import hermes_bridge, phone_cli, reminders
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: tmp_path))
    (tmp_path / 'config.yaml').write_text('{}')
    monkeypatch.setattr(RoutingConfig, 'load', classmethod(lambda cls: RoutingConfig.parse(routing_data())))
    monkeypatch.setattr(hermes_bridge, '_BRIDGES', [])
    registered, posts, jobs = {}, [], []

    def control(path, body=None):
        posts.append((path, body))
        return {'ok': True, 'callback': {'id': 'cb-test', 'reminder_job_id': 'job-1'},
                'warning': 'Bluetooth must be connected at the due time'}

    def cronjob(**args):
        jobs.append(args)
        return json.dumps({'success': True, 'job_id': 'job-1', 'message': 'Created'})

    monkeypatch.setattr(phone_cli, 'control', control)
    monkeypatch.setattr(reminders, 'telegram_destination', lambda: 'telegram')
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', lambda: {
        'platform': 'telegram', 'chat_id': 'owner', 'thread_id': None, 'deliver': 'telegram:owner'})
    monkeypatch.setattr(reminders, 'read_reminder', lambda job_id, due: {'verified': True, 'matches': True, 'run_at': due})
    original_control = control
    def verified_control(path, body=None):
        result = original_control(path, body)
        if path == '/v1/phone/callbacks': result['callback']['reminder_job_id'] = None
        if path.endswith('/reminder-claim'): result['acquired'] = True
        if body is None and path == '/v1/phone/callbacks/cb-test':
            result['callback']['run_at'] = posts[0][1]['run_at']
        return result
    monkeypatch.setattr(phone_cli, 'control', verified_control)
    monkeypatch.setitem(sys.modules, 'tools.cronjob_tools', SimpleNamespace(cronjob=cronjob))
    ctx = SimpleNamespace(profile_name='default', register_platform_handler=lambda *a: None,
                          register_hook=lambda *a: None,
                          register_tool=lambda **kw: registered.update({kw['name']: kw}))
    hermes_bridge.register(ctx)
    bridge = hermes_bridge._BRIDGES[-1]
    from tests.test_phone_workflows import FakeJobs
    bridge.workflows.jobs = FakeJobs(jobs)
    bridge.store.bind(session_id='hfp-admin', call_id='call', profile='default',
                      number='+919876543210', policy={'admin': True}, ttl=60,
                      timezone='Asia/Kolkata')
    bridge.store.bind(session_id='hfp-guest', call_id='guest', profile='default',
                      number='+919876543211', policy={'admin': False, 'tools': []}, ttl=60)
    yield registered, posts, jobs, bridge
    bridge.workflows.store.close()
    bridge.store.close()


def invoke(plugin, tool, args, sid='hfp-admin'):
    return json.loads(plugin[0][tool]['handler'](args, session_id=sid))


def test_phone_reminder_is_native_and_independent_of_bluetooth(plugin):
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'Appointment at 10:30 AM', 'delay_minutes': 5})
    assert result['ok'] and result['reminder']['error'] is None
    assert not plugin[1]
    job = plugin[2][0]
    assert job['deliver'] == 'telegram:owner'
    assert datetime.fromisoformat(job['schedule']).utcoffset().total_seconds() == 0
    assert 'final response' in job['prompt'] and 'Do not use tools' in job['prompt']


def test_callback_resolves_bound_number_and_has_one_native_executor(plugin):
    result = invoke(plugin, 'hfp_phone_schedule_call', {'purpose': 'Meeting', 'delay_minutes': 2})
    assert result['status'] == 'scheduled' and result['verification']['verified']
    assert result['callback']['number'] == '+919876543210'
    assert result['callback']['cron_job_id'] == result['callback']['reminder_job_id']
    assert not plugin[1] and len(plugin[2]) == 1 and plugin[2][0]['no_agent']
    assert invoke(plugin, 'hfp_phone_schedule_call', {'purpose': 'Meeting', 'delay_minutes': 2}, sid='owner-chat').get('error')


def test_missing_gateway_refuses_request_without_claiming_schedule_success(plugin, monkeypatch):
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', lambda: (_ for _ in ()).throw(ValueError('No gateway home')))
    result = invoke(plugin, 'hfp_phone_schedule_call', {'purpose': 'Meeting', 'delay_minutes': 2})
    assert not result['ok'] and result['status'] == 'unverified'
    assert not plugin[3].workflows.store.rows()
    assert not plugin[1] and not plugin[2]
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'Meeting', 'delay_minutes': 2})
    assert result['ok'] is False


def test_restricted_and_expired_phone_sessions_cannot_schedule_or_cancel(plugin):
    for sid in ['hfp-guest', 'hfp-expired']:
        for tool in ['hfp_phone_schedule_call', 'hfp_phone_schedule_reminder', 'hfp_phone_cancel_callback']:
            assert invoke(plugin, tool, {'purpose': 'test', 'delay_minutes': 1, 'callback_id': 'cb-test'}, sid).get('error')
    assert not plugin[1] and not plugin[2]


def test_cancel_pauses_native_call_without_hangup(plugin):
    created = invoke(plugin, 'hfp_phone_schedule_call', {'purpose': 'Meeting', 'delay_minutes': 2})
    result = invoke(plugin, 'hfp_phone_cancel_callback', {'callback_id': created['callback']['id']})
    assert result['ok'] and not result['active_call_ended']
    assert plugin[3].workflows.jobs.get(created['callback']['cron_job_id'])['state'] == 'paused'


@pytest.mark.parametrize('continued', [True, False])
def test_native_task_can_schedule_after_hangup_only_with_continuation(plugin, monkeypatch, continued):
    store = plugin[3].store
    parent = store.binding('hfp-admin')
    root = store.manage('a' * 32, 'hfp-admin')
    store.bind(session_id='hfp-child', call_id=parent['call_id'], profile='default',
               number='+919876543210', policy={'admin': True}, ttl=300)
    store.tasks.save(dict(task_id='task', request_id='req', conversation_id='a' * 32,
                          session_id=root, parent_binding='hfp-admin', binding_id='hfp-child',
                          run_id='run', status='running', continue_after_call=continued,
                          expires_at=time.time() + 300))
    store.bind_run('run', 'hfp-child', root)
    monkeypatch.setitem(sys.modules, 'tools.approval_context', SimpleNamespace(get_current_session_key=lambda: 'run'))
    store.revoke('hfp-admin')
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': '10:30 appointment', 'delay_minutes': 10}, root)
    assert bool(result.get('ok')) == continued
    assert len(plugin[2]) == int(continued)
    if continued:
        # Expired continuation is not standing permission for more actions.
        store.tasks.update('task', expires_at=time.time() - 1)
        assert invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'second', 'delay_minutes': 10}, root).get('error')
        assert len(plugin[2]) == 1


def test_bound_context_has_time_and_distinguishes_booking_from_memory(plugin):
    context = plugin[3].before_llm(session_id='hfp-admin')['context']
    assert 'Asia/Kolkata' in context and 'current_time' in context
    assert 'calendar event' in context and 'hfp_phone_schedule_reminder' in context


@pytest.mark.parametrize('args', [
    {}, {'delay_minutes': True}, {'delay_seconds': 0}, {'delay_minutes': 1, 'delay_seconds': 60},
    {'run_at': '2030-01-01T10:30:00'}, {'run_at': '2000-01-01T10:30:00+05:30'},
    {'run_at': ''}, {'delay_minutes': 99999999},
])
def test_invalid_or_ambiguous_schedule_has_no_side_effects(plugin, args):
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'meeting', **args})
    assert not result['ok'] and not plugin[1] and not plugin[2]


def test_timezone_offset_and_relative_delay(monkeypatch):
    monkeypatch.setattr(reminders.time, 'time', lambda: 1790360000)
    due = reminders.due_time({'run_at': '2026-09-26T10:20:00+05:30'})
    assert due == datetime.fromisoformat('2026-09-26T04:50:00+00:00').timestamp()
    assert reminders.due_time({'delay_minutes': 10}) == 1790360600


@pytest.mark.parametrize('mode,count', [('gateway', 1), ('call', 1), ('both', 2)])
def test_phone_reminder_defaults_select_components(plugin, monkeypatch, mode, count):
    from dataclasses import replace
    bridge = plugin[3]
    bridge.config = replace(bridge.config, reminder_delivery=mode)
    # The handlers share the configuration loaded at registration.
    data = routing_data()
    data['reminders'] = {'default_delivery': mode}
    # RoutingConfig is also used to revalidate the live authority.
    monkeypatch.setattr(RoutingConfig, 'load', classmethod(lambda cls: RoutingConfig.parse(data)))
    bridge.workflows.bridge.config = bridge.config
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'Cut your hair', 'delay_minutes': 3})
    assert result['ok'] and result['delivery'] == mode and len(plugin[2]) == count
    if mode != 'gateway':
        assert result['callback']['number'] == '+919876543210'
        assert plugin[2][0]['deliver'] == 'local'
    if mode == 'both':
        assert plugin[2][0]['schedule'] == plugin[2][1]['schedule']
        assert result['callback']['notification_job_id']


def test_explicit_call_only_works_without_gateway_and_is_idempotent(plugin, monkeypatch):
    def unavailable():
        raise AssertionError('Call-only scheduling must not resolve a gateway')
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', unavailable)
    args = {'purpose': 'Cut your hair', 'delay_minutes': 3, 'delivery': 'call', 'request_id': 'haircut'}
    first = invoke(plugin, 'hfp_phone_schedule_reminder', args)
    again = invoke(plugin, 'hfp_phone_schedule_reminder', args)
    assert first['ok'] and first == again
    assert first['destination'] is None and plugin[2][0]['deliver'] == 'local'
    assert len(plugin[2]) == 1
    changed = invoke(plugin, 'hfp_phone_schedule_reminder', {**args, 'delivery': 'both'})
    assert not changed['ok'] and len(plugin[2]) == 1


def test_explicit_gateway_overrides_call_default_and_freezes_native_target(plugin, monkeypatch):
    from dataclasses import replace
    plugin[3].config = replace(plugin[3].config, reminder_delivery='call')
    target = {'platform': 'slack', 'chat_id': 'owner', 'thread_id': 'thread', 'deliver': 'slack:owner:thread'}
    monkeypatch.setattr('hfp_mcp.hermes_compat.gateway_destination', lambda: target)
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {
        'purpose': 'Meeting', 'delay_minutes': 3, 'delivery': 'gateway'})
    assert result['ok'] and result['destination'] == target
    assert plugin[2][0]['deliver'] == target['deliver']
    assert not plugin[3].workflows.store.rows()


def test_chat_reminders_keep_gateway_default(plugin):
    from dataclasses import replace
    plugin[3].config = replace(plugin[3].config, reminder_delivery='both')
    result = invoke(plugin, 'hfp_phone_schedule_reminder', {'purpose': 'Meeting', 'delay_minutes': 3}, 'owner-chat')
    assert result['ok'] and result['delivery'] == 'gateway'
