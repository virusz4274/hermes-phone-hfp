"""Stable public contracts, validation, and idempotency helpers."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar


SCHEMA_VERSION = "hfp.v1"
CALLER_BINDING_TTL_SECONDS = 10.0
MAX_CALL_PURPOSE_CHARS = 4000
_MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
T = TypeVar("T")


class ContractError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def validate_call_purpose(value: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_CALL_PURPOSE_CHARS:
        raise ContractError("invalid_argument", "call purpose must be text of at most 4000 characters")
    return value.strip()


def validate_mac(value: str) -> str:
    candidate = value.strip().upper()
    if not _MAC_RE.fullmatch(candidate):
        raise ContractError("invalid_argument", "Bluetooth address must use AA:BB:CC:DD:EE:FF format")
    return candidate


def validate_request_id(value: str) -> str:
    candidate = value.strip()
    if not _REQUEST_ID_RE.fullmatch(candidate):
        raise ContractError("invalid_argument", "request_id must be 1-128 URL-safe characters")
    return candidate


def validate_session_id(value: str) -> str:
    candidate = value.strip()
    if not _SESSION_ID_RE.fullmatch(candidate):
        raise ContractError("invalid_argument", "session_id must be 1-128 URL-safe characters")
    return candidate


def normalize_phone_number(value: str, default_region: str | None = None) -> str:
    """Return an E.164 number and reject all AT-command metacharacters.

    ``phonenumbers`` is used when available so local numbers can be normalized.
    A strict E.164 fallback keeps the core importable in minimal test images.
    """
    candidate = value.strip()
    if any(ch in candidate for ch in ("\r", "\n", "\x00", ";")):
        raise ContractError("invalid_argument", "phone number contains a forbidden control character")
    try:
        import phonenumbers

        parsed = phonenumbers.parse(candidate, default_region or None)
        if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
            raise ContractError("invalid_argument", "phone number is not valid")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except ImportError:
        normalized = candidate.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not _E164_RE.fullmatch(normalized):
            raise ContractError("invalid_argument", "phone number must be E.164 when phonenumbers is unavailable")
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("invalid_argument", "phone number could not be parsed") from exc
    if not _E164_RE.fullmatch(normalized):
        raise ContractError("invalid_argument", "phone number must normalize to E.164")
    return normalized


def validate_timeout(value: float, *, maximum: float = 300.0) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_argument", "timeout_seconds must be numeric") from exc
    if timeout <= 0 or timeout > maximum:
        raise ContractError("invalid_argument", f"timeout_seconds must be between 0 and {maximum:g}")
    return timeout


def ok(result: Any = None, *, state: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": True}
    if result is not None:
        response["result"] = result
    if state is not None:
        response["state"] = state
    return response


def failure(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }
    if state is not None:
        response["state"] = state
    return response


@dataclass(frozen=True)
class CachedRequest:
    operation: str
    response: dict[str, Any]
    created_at: float


class RequestLedger:
    """Small SQLite ledger for idempotent public operations and action audit."""

    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.retention_days = max(1, retention_days)
        self._next_transcript_prune = 0.0
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                request_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                caller_number TEXT,
                role TEXT NOT NULL DEFAULT 'unknown',
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS call_audit_created_at ON call_audit(created_at);
            CREATE TABLE IF NOT EXISTS call_summary (
                call_id TEXT PRIMARY KEY,
                caller_number TEXT,
                role TEXT NOT NULL DEFAULT 'unknown',
                summary TEXT NOT NULL,
                started_at REAL,
                ended_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_transcript (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS call_transcript_call ON call_transcript(call_id,id);
            CREATE INDEX IF NOT EXISTS call_transcript_time ON call_transcript(created_at);
            """
        )
        from .conversations import prepare_schema
        prepare_schema(self._db)
        self._db.commit()
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.prune()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, request_id: str, operation: str) -> CachedRequest | None:
        request_id = validate_request_id(request_id)
        with self._lock:
            row = self._db.execute(
                "SELECT operation, response_json, created_at FROM idempotency WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != operation:
            raise ContractError("request_id_conflict", "request_id was already used for another operation")
        return CachedRequest(row[0], json.loads(row[1]), float(row[2]))

    def store(self, request_id: str, operation: str, response: dict[str, Any]) -> None:
        request_id = validate_request_id(request_id)
        payload = json.dumps(response, separators=(",", ":"), sort_keys=True)
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO idempotency(request_id,operation,response_json,created_at) VALUES(?,?,?,?)",
                    (request_id, operation, payload, time.time()),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                existing = self.get(request_id, operation)
                if existing is None or existing.response != response:
                    raise ContractError("request_id_conflict", "request_id completed with a different response")

    def audit(
        self,
        action: str,
        outcome: str,
        *,
        call_id: str | None = None,
        caller_number: str | None = None,
        role: str = "unknown",
        detail: dict[str, Any] | None = None,
    ) -> None:
        # Callers must pass already-redacted detail. Never put audio/transcripts here.
        with self._lock:
            self._db.execute(
                "INSERT INTO call_audit(call_id,caller_number,role,action,outcome,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    call_id,
                    caller_number,
                    role,
                    action,
                    outcome,
                    json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
                    time.time(),
                ),
            )
            self._db.commit()

    def save_call_summary(
        self,
        call_id: str,
        summary: str,
        *,
        caller_number: str | None = None,
        role: str = "unknown",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> None:
        if not call_id:
            raise ContractError("invalid_argument", "call_id is required for a summary")
        text = summary.strip()
        if len(text) > 16_000:
            text = text[:15_997] + "..."
        with self._lock:
            self._db.execute(
                """
                INSERT INTO call_summary(call_id,caller_number,role,summary,started_at,ended_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(call_id) DO UPDATE SET
                    caller_number=excluded.caller_number,
                    role=excluded.role,
                    summary=excluded.summary,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at
                """,
                (
                    call_id,
                    caller_number,
                    role,
                    text,
                    started_at,
                    time.time() if ended_at is None else ended_at,
                ),
            )
            self._db.commit()

    def get_call_summary(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT caller_number,role,summary,started_at,ended_at FROM call_summary WHERE call_id=?",
                (call_id,),
            ).fetchone()
        if row is None:
            return None
        memory = None
        with self._lock:
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='call_memory_runs'").fetchone():
                saved = self._db.execute("SELECT data FROM call_memory_runs WHERE call_id=?", (call_id,)).fetchone()
                memory = json.loads(saved[0]).get("result") if saved else None
        return {
            **({"memory_save": memory} if memory else {}),
            "call_id": call_id,
            "caller_number": row[0],
            "role": row[1],
            "summary": row[2],
            "started_at": row[3],
            "ended_at": row[4],
        }

    def prune(self) -> int:
        cutoff = time.time() - (self.retention_days * 86400)
        with self._lock:
            counts = 0
            for table, column in (
                ("idempotency", "created_at"),
                ("call_audit", "created_at"),
                ("call_summary", "ended_at"),
            ):
                cursor = self._db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                counts += max(0, cursor.rowcount)
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='call_memory_runs'").fetchone():
                self._db.execute("DELETE FROM call_memory_runs WHERE json_extract(data,'$.updated_at')<? "
                                 "AND COALESCE(json_extract(data,'$.receipt.deadline'),0)<?", (cutoff, time.time()))
            self._db.execute("DELETE FROM call_transcript WHERE "
                "(conversation_id IS NULL AND created_at<?) OR conversation_id IN "
                "(SELECT id FROM phone_conversations WHERE archived_at<?)",
                (cutoff, time.time() - 30*86400))
            self._db.commit()
        return counts

    def append_transcript(self, event: dict[str, Any]) -> None:
        """Persist an explicitly opted-in transcription event, separate from audit."""
        if time.time() >= self._next_transcript_prune:
            self.prune()
            self._next_transcript_prune = time.time() + 3600
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO call_transcript(call_id,provider,direction,text,metadata_json,created_at,conversation_id,event_id) VALUES(?,?,?,?,?,?,?,?)",
                (event["session_id"], event["provider"], event["direction"], event["text"],
                 json.dumps(event.get("metadata", {})), event["timestamp"],
                 event.get("conversation_id"), event.get("event_id")),
            )
            self._db.commit()

    def transcript_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        self.prune()
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id,MIN(created_at),MAX(created_at),COUNT(*) FROM call_transcript "
                "GROUP BY call_id ORDER BY MAX(created_at) DESC LIMIT ?",
                (min(200, max(1, limit)),),
            ).fetchall()
        return [dict(zip(("call_id", "first_event_at", "last_event_at", "events"), row)) for row in rows]

    def transcript(self, call_id: str | None = None, *, after_id: int = 0, limit: int = 500) -> dict:
        self.prune()
        limit = min(1000, max(1, limit))
        with self._lock:
            if call_id is None:
                latest = self._db.execute("SELECT call_id FROM call_transcript ORDER BY id DESC LIMIT 1").fetchone()
                call_id = latest[0] if latest else None
            rows = self._db.execute(
                "SELECT id,provider,direction,text,metadata_json,created_at FROM call_transcript "
                "WHERE call_id=? AND id>? ORDER BY id LIMIT ?", (call_id, max(0, after_id), limit + 1),
            ).fetchall()
        events = [
            {"id": r[0], "session_id": call_id, "provider": r[1], "direction": r[2],
             "text": r[3], "metadata": json.loads(r[4]), "timestamp": r[5]}
            for r in rows[:limit]
        ]
        return {"ok": True, "session_id": call_id, "events": events,
                "has_more": len(rows) > limit,
                "next_after_id": events[-1]["id"] if events else after_id,
                "storage": "persistent"}


def run_idempotent(
    ledger: RequestLedger,
    request_id: str,
    operation: str,
    function: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    cached = ledger.get(request_id, operation)
    if cached is not None:
        return cached.response
    response = function()
    ledger.store(request_id, operation, response)
    return response
