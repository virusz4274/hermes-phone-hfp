import asyncio
import sqlite3
import time

import pytest

from hfp_mcp.callbacks import CallbackScheduler


def make_scheduler(tmp_path, reconcile=None):
    scheduler = CallbackScheduler(tmp_path / 'calls.db', None, reconcile_reminder=reconcile)
    row = scheduler.schedule(request_id='create', number='+919876543210', purpose='Meeting', run_at=time.time() + 60)
    return scheduler, row


async def test_change_preserves_ids_and_old_time_never_fires(tmp_path, monkeypatch):
    calls = []
    async def move(row):
        calls.append(row['reminder_job_id'])
        return {'verified': True, 'run_at': row['schedule_change']['requested_run_at']}
    s, first = make_scheduler(tmp_path, move)
    s.link_reminder(first['id'], 'existing-job')
    first = s.get(first['id'])
    target = first['run_at'] + 600
    args = dict(request_id='move', expected_revision=first['revision'], run_at=target)
    held = s.begin_reschedule(first['id'], **args)
    assert held['status'] == 'rescheduling'
    monkeypatch.setattr('hfp_mcp.callbacks.time.time', lambda: first['run_at'] + 1)
    assert s._claim_due() is None
    moved = await s.reconcile(first['id'])
    assert moved['id'] == first['id'] and moved['reminder_job_id'] == 'existing-job'
    assert moved['run_at'] == target and moved['schedule_change']['status'] == 'applied'
    assert s._claim_due() is None
    assert s.begin_reschedule(first['id'], **args) == moved
    assert await s.reconcile(first['id']) == moved and calls == ['existing-job']
    with pytest.raises(ValueError): s.begin_reschedule(first['id'], **{**args, 'run_at': target + 1})
    # An old request keeps its own immutable receipt after a subsequent change.
    newer = s.begin_reschedule(first['id'], request_id='second', expected_revision=moved['revision'], run_at=target + 600)
    await s.reconcile(first['id'])
    assert s.begin_reschedule(first['id'], **args) == moved
    assert newer['revision'] > moved['revision']
    s.close_db()


@pytest.mark.parametrize('failure', ['timeout', 'mismatch', 'rejected'])
async def test_partial_outcomes_are_truthful_and_restart_safe(tmp_path, failure):
    async def move(row):
        if failure == 'timeout': raise TimeoutError()
        return {'verified': True, 'run_at': row['run_at'] if failure == 'rejected' else row['run_at'] + 1,
                'write_failed': failure == 'rejected'}
    s, first = make_scheduler(tmp_path, move)
    s.link_reminder(first['id'], 'job')
    first = s.get(first['id'])
    target = first['run_at'] + 600
    s.begin_reschedule(first['id'], request_id='move', expected_revision=first['revision'], run_at=target)
    result = await s.reconcile(first['id'])
    assert result['run_at'] == first['run_at']
    assert result['schedule_change']['requested_run_at'] == target
    assert result['schedule_change']['status'] == {'timeout': 'uncertain', 'mismatch': 'partial', 'rejected': 'failed'}[failure]
    assert result['status'] == ('scheduled' if failure == 'rejected' else 'rescheduling')
    s.close_db()
    recovered = CallbackScheduler(tmp_path / 'calls.db', None, reconcile_reminder=lambda _: None)
    assert recovered.get(first['id'])['schedule_change'] == result['schedule_change']
    if failure != 'rejected':
        async def read_back(row): return {'verified': True, 'run_at': target}
        recovered.reconcile_reminder = read_back
        assert (await recovered.reconcile(first['id']))['run_at'] == target
    recovered.close_db()


async def test_unlinked_callback_moves_and_missing_reminder_remains_visible(tmp_path):
    s, first = make_scheduler(tmp_path)
    s.begin_reschedule(first['id'], request_id='move', expected_revision=0, run_at=first['run_at'] + 60)
    result = await s.reconcile(first['id'])
    assert result['schedule_change']['status'] == 'applied' and result['reminder_job_id'] is None
    s.close_db()


@pytest.mark.parametrize('state', ['firing', 'completed', 'failed', 'cancelled', 'uncertain'])
def test_terminal_and_stale_changes_do_not_write(tmp_path, state):
    s, first = make_scheduler(tmp_path)
    with pytest.raises(ValueError): s.begin_reschedule(first['id'], request_id='stale', expected_revision=1, run_at=first['run_at'] + 60)
    with s._db: s._db.execute('UPDATE phone_callbacks SET status=?', (state,))
    with pytest.raises(ValueError): s.begin_reschedule(first['id'], request_id='move', expected_revision=0, run_at=first['run_at'] + 60)
    assert s._db.execute('SELECT COUNT(*) FROM callback_operations').fetchone()[0] == 0
    s.close_db()


async def test_cancel_and_link_cannot_race_held_change(tmp_path):
    s, first = make_scheduler(tmp_path)
    s.begin_reschedule(first['id'], request_id='move', expected_revision=0, run_at=first['run_at'] + 60)
    with pytest.raises(ValueError): s.cancel(first['id'])
    with pytest.raises(ValueError): s.link_reminder(first['id'], 'new-job')
    s.close_db()


def test_migration_preserves_old_callbacks(tmp_path):
    db = sqlite3.connect(tmp_path / 'calls.db')
    db.execute('''CREATE TABLE phone_callbacks (id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,
        number TEXT NOT NULL,purpose TEXT NOT NULL,run_at REAL NOT NULL,status TEXT NOT NULL,
        result_json TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL)''')
    db.execute("INSERT INTO phone_callbacks VALUES ('cb-old','old','+919876543210','Meeting',?,'scheduled',NULL,NULL,1,1)", (time.time() + 60,))
    db.commit(); db.close()
    s = CallbackScheduler(tmp_path / 'calls.db', None)
    assert s.get('cb-old')['revision'] == 0 and s.get('cb-old')['purpose'] == 'Meeting'
    s.close_db()


async def test_http_reschedule_readback_validation_and_conflict(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from hfp_mcp.http_routes import HttpRuntime, control_routes
    s, first = make_scheduler(tmp_path)
    runtime = HttpRuntime(None, lambda: None, lambda: None, lambda: None, lambda: None,
                          lambda: {}, lambda: {}, None, callbacks=lambda: s)
    client = TestClient(Starlette(routes=control_routes(None, runtime)))
    path = '/v1/phone/callbacks/' + first['id']
    with client:
        assert client.get(path).json()['callback']['revision'] == 0
        body = {'request_id': 'move', 'expected_revision': 0,
                'run_at': datetime.fromtimestamp(first['run_at'] + 600, timezone.utc).isoformat()}
        result = client.post(path + '/reschedule', json=body)
        assert result.status_code == 200 and result.json()['verification']['status'] == 'applied'
        assert result.json()['verification']['reminder_missing'] is True
        assert client.post(path + '/reschedule', json=body).json() == result.json()
        stale = {**body, 'request_id': 'stale'}
        assert client.post(path + '/reschedule', json=stale).status_code == 409
        for invalid in [None, 3, '2026-10-07T10:00:00']:
            assert client.post(path + '/reschedule', json={**body, 'request_id': 'bad', 'run_at': invalid}).status_code == 400
    s.close_db()
