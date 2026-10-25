"""Legacy callback archive and compatibility migration primitives.

Production no longer starts this scheduler. Native Hermes cron owns future calls;
legacy pending rows are held for explicit review and terminal history is retained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .contracts import normalize_phone_number, validate_call_purpose, validate_request_id


TERMINAL = {"completed", "failed", "uncertain", "cancelled"}
log = logging.getLogger(__name__)


class CallbackConflict(ValueError):
    """A callback changed or is no longer eligible for a schedule mutation."""


class CallbackScheduler:
    """Persist and fire one-shot callbacks without replaying uncertain calls."""

    def __init__(self, path: Path, fire, *, region: str = "IN", reconcile_reminder=None) -> None:
        self.path = Path(path)
        self.fire = fire
        self.region = region
        self.reconcile_reminder = reconcile_reminder
        self._operation_locks = {}
        self._next_reconciliation = 0.0
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS phone_callbacks (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                number TEXT NOT NULL,
                purpose TEXT NOT NULL,
                run_at REAL NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS phone_callbacks_due
                ON phone_callbacks(status, run_at);
            """
        )
        with self._db:
            if 'reminder_job_id' not in {r[1] for r in self._db.execute('PRAGMA table_info(phone_callbacks)')}:
                self._db.execute('ALTER TABLE phone_callbacks ADD COLUMN reminder_job_id TEXT')
            if 'revision' not in {r[1] for r in self._db.execute('PRAGMA table_info(phone_callbacks)')}:
                self._db.execute('ALTER TABLE phone_callbacks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            if 'verification_json' not in {r[1] for r in self._db.execute('PRAGMA table_info(phone_callbacks)')}:
                self._db.execute('ALTER TABLE phone_callbacks ADD COLUMN verification_json TEXT')
            if 'reminder_attempted' not in {r[1] for r in self._db.execute('PRAGMA table_info(phone_callbacks)')}:
                self._db.execute('ALTER TABLE phone_callbacks ADD COLUMN reminder_attempted INTEGER NOT NULL DEFAULT 0')
            self._db.execute('''CREATE TABLE IF NOT EXISTS callback_operations (
                request_id TEXT PRIMARY KEY, callback_id TEXT NOT NULL,
                expected_revision INTEGER NOT NULL, requested_run_at REAL NOT NULL,
                previous_run_at REAL NOT NULL, status TEXT NOT NULL,
                reminder_run_at REAL, error TEXT, updated_at REAL NOT NULL)''')
            if 'response_json' not in {r[1] for r in self._db.execute('PRAGMA table_info(callback_operations)')}:
                self._db.execute('ALTER TABLE callback_operations ADD COLUMN response_json TEXT')
        # A daemon restart while a callback was being fired leaves its outcome
        # unknowable.  Never dial it a second time automatically.
        now = time.time()
        with self._db:
            self._db.execute(
                "UPDATE phone_callbacks SET status='uncertain', error=?, updated_at=? "
                "WHERE status='firing'",
                ("Daemon restarted while the callback was being fired; call outcome is uncertain.", now),
            )
        self.path.chmod(0o600)
        self._worker: asyncio.Task | None = None
        self._children: set[asyncio.Task] = set()

    def close_db(self) -> None:
        with self._lock:
            self._db.close()

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._watch(), name="hfp-phone-callbacks")

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
        for task in self._children:
            task.cancel()
        await asyncio.gather(
            *([self._worker] if self._worker else []),
            *self._children,
            return_exceptions=True,
        )
        self.close_db()

    def schedule(self, *, request_id: str, number: str, purpose: str, run_at: float) -> dict:
        if not isinstance(request_id, str):
            raise ValueError('request_id must be text')
        request_id = validate_request_id(request_id)
        number = normalize_phone_number(number, self.region)
        purpose = validate_call_purpose(purpose)
        if not purpose:
            raise ValueError("callback purpose is required")
        try:
            run_at = float(run_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("run_at must be a Unix timestamp") from exc
        callback_id = "cb-" + request_id
        now = time.time()
        row = {
            "id": callback_id,
            "request_id": request_id,
            "number": number,
            "purpose": purpose,
            "run_at": run_at,
            "status": "scheduled",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "reminder_job_id": None,
            "revision": 0,
            "verification": None,
        }
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT * FROM phone_callbacks WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                old = self._row(existing)
                if any(old[key] != row[key] for key in ("number", "purpose", "run_at")):
                    raise ValueError("request_id was already used for another callback")
                return old
            if not now < run_at <= now + 366 * 24 * 60 * 60:
                raise ValueError("run_at must be in the future and within one year")
            self._db.execute(
                "INSERT INTO phone_callbacks "
                "(id,request_id,number,purpose,run_at,status,result_json,error,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (callback_id, request_id, number, purpose, run_at, "scheduled", None, None, now, now),
            )
            return self.get(callback_id)

    def get(self, callback_id):
        with self._lock:
            row = self._db.execute('SELECT * FROM phone_callbacks WHERE id=?', (callback_id,)).fetchone()
            if not row:
                raise ValueError('unknown callback')
            result = self._row(row)
            operation = self._db.execute('SELECT request_id,requested_run_at,previous_run_at,status,reminder_run_at,error '
                'FROM callback_operations WHERE callback_id=? ORDER BY updated_at DESC LIMIT 1', (callback_id,)).fetchone()
            if operation:
                result['schedule_change'] = dict(zip(
                    ('request_id', 'requested_run_at', 'previous_run_at', 'status', 'reminder_run_at', 'error'), operation))
                result['schedule_change']['requested_run_at_iso'] = datetime.fromtimestamp(operation[1], timezone.utc).isoformat()
            return result

    def begin_reschedule(self, callback_id, *, request_id, expected_revision, run_at):
        if not isinstance(request_id, str):
            raise ValueError('request_id must be text')
        request_id = validate_request_id(request_id)
        if type(expected_revision) is not int or not isinstance(run_at, (int, float)) or isinstance(run_at, bool) or not math.isfinite(run_at):
            raise ValueError('expected_revision must be an integer and run_at a finite timestamp')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            old = self._db.execute('SELECT callback_id,expected_revision,requested_run_at,response_json FROM callback_operations WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old[:3] != (callback_id, expected_revision, run_at):
                    raise ValueError('request_id was already used for another schedule change')
                return json.loads(old[3]) if old[3] else self.get(callback_id)
            row = self.get(callback_id)
            if row['revision'] != expected_revision or row['status'] != 'scheduled':
                raise CallbackConflict('callback revision changed or callback is firing, terminal, or being rescheduled')
            attempted = self._db.execute('SELECT reminder_attempted FROM phone_callbacks WHERE id=?', (callback_id,)).fetchone()[0]
            if attempted and row['reminder_job_id'] is None:
                raise CallbackConflict('reminder creation or linkage is unresolved; inspect the original schedule before moving it')
            if not time.time() < run_at <= time.time() + 366 * 86400:
                raise ValueError('run_at must be in the future and within one year')
            self._db.execute('INSERT INTO callback_operations (request_id,callback_id,expected_revision,requested_run_at,previous_run_at,status,reminder_run_at,error,updated_at) VALUES (?,?,?,?,?,?,NULL,NULL,?)',
                (request_id, callback_id, expected_revision, run_at, row['run_at'], 'pending', time.time()))
            self._db.execute("UPDATE phone_callbacks SET status='rescheduling',revision=revision+1,updated_at=? WHERE id=?", (time.time(), callback_id))
            return self.get(callback_id)

    def record_verification(self, callback_id, expected_revision, verification):
        if not isinstance(verification, dict) or type(expected_revision) is not int:
            raise ValueError('invalid schedule verification')
        with self._lock, self._db:
            row = self.get(callback_id)
            if row['revision'] != expected_revision or row['status'] != 'scheduled':
                raise CallbackConflict('callback changed before verification was recorded')
            self._db.execute('UPDATE phone_callbacks SET verification_json=? WHERE id=?',
                (json.dumps({**verification, 'observed_at': time.time()}), callback_id))
        return self.get(callback_id)

    async def reconcile(self, callback_id):
        async with self._operation_locks.setdefault(callback_id, asyncio.Lock()):
            row = self.get(callback_id)
            operation = row.get('schedule_change')
            if not operation or operation['status'] in {'applied', 'failed'}:
                return row
            requested = operation['requested_run_at']
            try:
                if row['reminder_job_id']:
                    if self.reconcile_reminder is None:
                        raise RuntimeError('reminder reconciliation unavailable')
                    observed = await self.reconcile_reminder(row)
                else:
                    observed = {'verified': True, 'run_at': None, 'missing': True}
                reminder_time = observed.get('run_at')
                from .reminders import same_instant
                verified = observed.get('verified') is True
                success = verified and (observed.get('missing') or same_instant(reminder_time, requested))
                old_unchanged = verified and same_instant(reminder_time, operation['previous_run_at']) and observed.get('write_failed')
                status = 'applied' if success else 'failed' if old_unchanged else 'partial'
                error = None if success else observed.get('error', 'Reminder schedule could not be verified')
            except asyncio.CancelledError:
                # The durable pending receipt prevents firing and is reconciled after restart.
                raise
            except Exception:
                status, reminder_time, error = 'uncertain', None, 'Reminder update outcome is unverified; callback remains held'
            with self._lock, self._db:
                self._db.execute('BEGIN IMMEDIATE')
                self._db.execute('UPDATE callback_operations SET status=?,reminder_run_at=?,error=?,updated_at=? WHERE request_id=?',
                    (status, reminder_time, error, time.time(), operation['request_id']))
                if status in {'applied', 'failed'}:
                    effective = requested if status == 'applied' else operation['previous_run_at']
                    self._db.execute("UPDATE phone_callbacks SET status='scheduled',run_at=?,error=?,revision=revision+1,updated_at=? WHERE id=? AND status='rescheduling'",
                        (effective, error, time.time(), callback_id))
                self._db.execute('UPDATE phone_callbacks SET verification_json=? WHERE id=?',
                    (json.dumps({'status': status, 'requested_run_at': requested,
                        'callback_run_at': self.get(callback_id)['run_at'], 'reminder_run_at': reminder_time,
                        'verified': status == 'applied', 'observed_at': time.time()}), callback_id))
                if status in {'applied', 'failed'}:
                    snapshot = self.get(callback_id)
                    self._db.execute('UPDATE callback_operations SET response_json=? WHERE request_id=?',
                        (json.dumps(snapshot), operation['request_id']))
            return self.get(callback_id)

    def cancel(self, callback_id):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE phone_callbacks SET status='cancelled', revision=revision+1, updated_at=? WHERE id=? AND status='scheduled'",
                (time.time(), callback_id),
            )
            row = self._db.execute('SELECT * FROM phone_callbacks WHERE id=?', (callback_id,)).fetchone()
            if not row:
                raise ValueError('unknown callback')
            result = self._row(row)
            if result['status'] != 'cancelled':
                raise ValueError('callback is already firing or terminal; cancellation was not applied')
            return result

    def link_reminder(self, callback_id, job_id):
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            raise ValueError('invalid reminder job ID')
        with self._lock, self._db:
            updated = self._db.execute(
                "UPDATE phone_callbacks SET revision=revision+CASE WHEN reminder_job_id IS NULL THEN 1 ELSE 0 END,reminder_job_id=? WHERE id=? AND status='scheduled' AND (reminder_job_id IS NULL OR reminder_job_id=?)",
                (job_id, callback_id, job_id),
            )
            if not updated.rowcount:
                raise ValueError('unknown callback or reminder already linked')

    def claim_reminder(self, callback_id):
        # An interrupted creation is uncertain, never permission to duplicate it.
        with self._lock, self._db:
            return bool(self._db.execute("UPDATE phone_callbacks SET reminder_attempted=1 WHERE id=? "
                "AND status='scheduled' AND reminder_job_id IS NULL AND reminder_attempted=0", (callback_id,)).rowcount)

    def list(self, *, include_terminal: bool = True) -> list[dict]:
        query = "SELECT * FROM phone_callbacks"
        args: tuple = ()
        if not include_terminal:
            query += " WHERE status NOT IN (?,?,?,?)"
            args = tuple(TERMINAL)
        query += " ORDER BY run_at, created_at"
        with self._lock:
            return [self.get(row[0]) for row in self._db.execute(query, args).fetchall()]

    @staticmethod
    def _row(row) -> dict:
        result = json.loads(row[6]) if row[6] else None
        error = row[7]
        if not error and row[5] == 'failed' and isinstance(result, dict):
            detail = result.get('error')
            error = (detail.get('message') or detail.get('code')) if isinstance(detail, dict) else detail
            error = error or result.get('message')
        return {
            "id": row[0],
            "request_id": row[1],
            "number": row[2],
            "purpose": row[3],
            "run_at": row[4],
            "run_at_iso": datetime.fromtimestamp(row[4], timezone.utc).isoformat(),
            "status": row[5],
            "result": result,
            "error": error,
            "created_at": row[8],
            "updated_at": row[9],
            "reminder_job_id": row[10],
            "revision": row[11],
            "verification": json.loads(row[12]) if row[12] else None,
        }

    def _claim_due(self) -> dict | None:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            # Avoid surprise calls hours/days late when a stopped host returns.
            now = time.time()
            self._db.execute(
                "UPDATE phone_callbacks SET status='failed', error=?, updated_at=? "
                "WHERE status='scheduled' AND run_at<?",
                ('Callback missed its five-minute firing window; no call was placed.', now, now - 300),
            )
            row = self._db.execute(
                "SELECT * FROM phone_callbacks WHERE status='scheduled' AND run_at<=? "
                "ORDER BY run_at, created_at LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                self._db.commit()
                return None
            now = time.time()
            self._db.execute(
                "UPDATE phone_callbacks SET status='firing', revision=revision+1, updated_at=? WHERE id=?",
                (now, row[0]),
            )
            self._db.commit()
            claimed = self._db.execute("SELECT * FROM phone_callbacks WHERE id=?", (row[0],)).fetchone()
            return self._row(claimed)

    def _finish(self, callback_id: str, *, status: str, result=None, error=None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE phone_callbacks SET status=?, result_json=?, error=?, updated_at=? WHERE id=?",
                (status, json.dumps(result, sort_keys=True) if result is not None else None,
                 str(error)[:2000] if error else None, time.time(), callback_id),
            )

    async def _watch(self) -> None:
        while True:
            if self.reconcile_reminder and time.monotonic() >= self._next_reconciliation:
                self._next_reconciliation = time.monotonic() + 10
                for row in self.list(include_terminal=False):
                    if row['status'] == 'rescheduling' and not self._operation_locks.get(row['id'], asyncio.Lock()).locked():
                        task = asyncio.create_task(self.reconcile(row['id']), name='hfp-reschedule-recovery')
                        self._children.add(task)
                        task.add_done_callback(self._children.discard)
            try:
                callback = self._claim_due()
            except sqlite3.OperationalError:
                # Another daemon ledger writer can hold the shared database.
                # Retry only the claim transaction, never an already fired call.
                log.exception('Callback queue unavailable; will check again')
                await asyncio.sleep(1)
                continue
            if callback:
                task = asyncio.create_task(self._fire(callback), name=f"hfp-callback-{callback['id']}")
                self._children.add(task)
                task.add_done_callback(self._children.discard)
                continue
            await asyncio.sleep(0.5)

    async def _fire(self, callback: dict) -> None:
        try:
            result = await self.fire(
                callback["number"], callback["request_id"], callback["purpose"]
            )
            if isinstance(result, dict) and result.get("ok"):
                self._finish(callback["id"], status="completed", result=result)
            else:
                error = result.get('error') if isinstance(result, dict) else result
                if isinstance(error, dict):
                    error = error.get('message') or error.get('code')
                if not error and isinstance(result, dict):
                    error = result.get('message')
                self._finish(callback["id"], status="failed", result=result,
                             error=error or 'Call did not report success')
        except asyncio.CancelledError:
            # The in-flight outcome cannot be known after cancellation.
            self._finish(callback["id"], status="uncertain",
                         error="Callback worker stopped while the call outcome was uncertain.")
            raise
        except Exception as exc:
            self._finish(callback["id"], status="failed", error=exc)
