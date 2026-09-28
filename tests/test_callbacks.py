import asyncio
import time

import pytest

from hfp_mcp.callbacks import CallbackScheduler


@pytest.mark.asyncio
async def test_callback_is_persistent_and_fires_once(tmp_path):
    fired = []

    async def fire(number, request_id, purpose):
        fired.append((number, request_id, purpose))
        return {"ok": True, "call_id": "call-1"}

    scheduler = CallbackScheduler(tmp_path / "calls.db", fire, region="IN")
    first = scheduler.schedule(
        request_id="request-1", number="+919876543210", purpose="Meeting reminder",
        run_at=time.time() + 0.5,
    )
    assert scheduler.schedule(
        request_id="request-1", number="+919876543210", purpose="Meeting reminder",
        run_at=first["run_at"],
    )["id"] == first["id"]
    await scheduler.start()
    await asyncio.sleep(0.8)
    assert fired == [("+919876543210", "request-1", "Meeting reminder")]
    assert scheduler.list()[0]["status"] == "completed"
    await scheduler.close()


@pytest.mark.asyncio
async def test_callback_failure_is_not_retried(tmp_path):
    calls = 0

    async def fire(*_args):
        nonlocal calls
        calls += 1
        return {"ok": False, "message": "phone busy"}

    scheduler = CallbackScheduler(tmp_path / "calls.db", fire)
    scheduler.schedule(
        request_id="request-2", number="+919876543210", purpose="Appointment",
        run_at=time.time() + 0.5,
    )
    await scheduler.start()
    await asyncio.sleep(0.8)
    assert calls == 1
    assert scheduler.list()[0]["status"] == "failed"
    await scheduler.close()


def test_callback_restart_marks_inflight_uncertain(tmp_path):
    async def fire(*_args):
        return {"ok": True}

    scheduler = CallbackScheduler(tmp_path / "calls.db", fire)
    scheduler.schedule(
        request_id="request-3", number="+919876543210", purpose="Appointment",
        run_at=time.time() + 60,
    )
    scheduler._db.execute("UPDATE phone_callbacks SET status='firing'")
    scheduler._db.commit()
    scheduler.close_db()
    restarted = CallbackScheduler(tmp_path / "calls.db", fire)
    assert restarted.list()[0]["status"] == "uncertain"
    restarted.close_db()


async def test_pending_callback_survives_restart_and_records_nested_dial_failure(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr('hfp_mcp.callbacks.time.time', lambda: now)
    calls = []
    async def fire(*args):
        calls.append(args)
        return {'ok': False, 'error': {'code': 'command_rejected', 'message': 'Phone not connected'}}
    path = tmp_path / 'calls.db'
    first = CallbackScheduler(path, fire)
    args = dict(request_id='persist', number='+919876543210', purpose='Meeting', run_at=now + 60)
    first.schedule(**args)
    first.close_db()
    later = CallbackScheduler(path, fire)
    now += 61
    claimed = later._claim_due()
    await later._fire(claimed)
    assert later.list()[0]['error'] == 'Phone not connected'
    assert later.schedule(**args)['status'] == 'failed'  # Replay after the due time.
    assert later._claim_due() is None and len(calls) == 1
    later.close_db()


def test_cancelled_or_stale_callback_never_dials_and_late_cancel_is_truthful(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr('hfp_mcp.callbacks.time.time', lambda: now)
    scheduler = CallbackScheduler(tmp_path / 'calls.db', None)
    for key in ['cancel', 'stale', 'firing']:
        scheduler.schedule(request_id=key, number='+919876543210', purpose='Appointment', run_at=now + 60)
    scheduler.link_reminder('cb-cancel', 'job-1')
    assert scheduler.cancel('cb-cancel')['reminder_job_id'] == 'job-1'
    assert scheduler.cancel('cb-cancel')['status'] == 'cancelled'
    now += 61
    assert scheduler._claim_due()['id'] == 'cb-stale'
    with pytest.raises(ValueError, match='already firing'):
        scheduler.cancel('cb-stale')
    now += 600
    assert scheduler._claim_due() is None
    assert next(r for r in scheduler.list() if r['id'] == 'cb-firing')['error'].startswith('Callback missed')
    scheduler.close_db()
