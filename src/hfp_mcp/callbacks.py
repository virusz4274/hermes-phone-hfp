"""Durable, one-shot outbound phone callbacks.

Hermes cron is the right owner for text/card delivery, but a cron run is not an
authenticated phone call session.  Phone callbacks therefore live in the HFP
daemon and are fired only after an authenticated owner request has persisted
them here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .contracts import normalize_phone_number, validate_call_purpose, validate_request_id


TERMINAL = {"completed", "failed", "uncertain", "cancelled"}
log = logging.getLogger(__name__)


class CallbackScheduler:
    """Persist and fire one-shot callbacks without replaying uncertain calls."""

    def __init__(self, path: Path, fire, *, region: str = "IN") -> None:
        self.path = Path(path)
        self.fire = fire
        self.region = region
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
        return row

    def cancel(self, callback_id):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE phone_callbacks SET status='cancelled', updated_at=? WHERE id=? AND status='scheduled'",
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
                'UPDATE phone_callbacks SET reminder_job_id=? WHERE id=? AND (reminder_job_id IS NULL OR reminder_job_id=?)',
                (job_id, callback_id, job_id),
            )
            if not updated.rowcount:
                raise ValueError('unknown callback or reminder already linked')

    def list(self, *, include_terminal: bool = True) -> list[dict]:
        query = "SELECT * FROM phone_callbacks"
        args: tuple = ()
        if not include_terminal:
            query += " WHERE status NOT IN (?,?,?,?)"
            args = tuple(TERMINAL)
        query += " ORDER BY run_at, created_at"
        with self._lock:
            return [self._row(row) for row in self._db.execute(query, args).fetchall()]

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
            "status": row[5],
            "result": result,
            "error": error,
            "created_at": row[8],
            "updated_at": row[9],
            "reminder_job_id": row[10],
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
                "UPDATE phone_callbacks SET status='firing', updated_at=? WHERE id=?",
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
