"""Caller-scoped notes and expiring call authority, independent of shared memory."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .security import load_or_create_token
from .contracts import CALLER_BINDING_TTL_SECONDS


class NoteConflict(ValueError):
    """The supplied note revision is stale; read and merge again."""


class CallerStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._key = load_or_create_token(path.with_suffix(".key")).encode()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if path.stat().st_mode & 0o077:
            raise PermissionError("caller database must have mode 0600")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                profile TEXT NOT NULL, caller_id TEXT NOT NULL, note TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(profile, caller_id));
            CREATE TABLE IF NOT EXISTS bindings (
                session_id TEXT PRIMARY KEY, call_id TEXT NOT NULL,
                profile TEXT NOT NULL, caller_id TEXT NOT NULL,
                policy TEXT NOT NULL, persistent INTEGER NOT NULL,
                expires REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS managed_sessions (
                root_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL UNIQUE,
                profile TEXT NOT NULL, caller_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS run_bindings (
                run_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, root_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS note_versions (
                profile TEXT NOT NULL, caller_id TEXT NOT NULL, revision INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(profile, caller_id));
        """)
        self.db.commit()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(bindings)')}
            if 'started_at' not in columns:
                self.db.execute('ALTER TABLE bindings ADD COLUMN started_at REAL')
            if 'timezone' not in columns:
                self.db.execute("ALTER TABLE bindings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        from .phone_tasks import TaskRegistry
        self.tasks = TaskRegistry(self)

    def caller_id(self, number: str) -> str:
        return hmac.new(self._key, number.encode(), hashlib.sha256).hexdigest()[:32]

    def bind(
        self,
        *,
        session_id: str,
        call_id: str,
        profile: str,
        number: str | None,
        policy: dict,
        ttl: float,
        remember: bool = True,
        anonymous_identity: str | None = None,
        timezone: str = 'UTC',
    ) -> dict:
        caller_id = self.caller_id(number) if number else (anonymous_identity or secrets.token_hex(16))
        from zoneinfo import ZoneInfo
        ZoneInfo(timezone)
        with self._lock, self.db:
            existing = self.db.execute(
                "SELECT call_id FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing:
                raise ValueError("session already bound; authority cannot be replaced")
            self.db.execute(
                "DELETE FROM bindings WHERE expires < ?", (time.time() - 86400,)
            )
            self.db.execute(
                "INSERT INTO bindings (session_id,call_id,profile,caller_id,policy,persistent,expires,active) VALUES (?,?,?,?,?,?,?,1)",
                (
                    session_id,
                    call_id,
                    profile,
                    caller_id,
                    json.dumps(policy),
                    int(bool(number) and remember),
                    time.time() + ttl,
                ),
            )
            # Request leases for a call inherit its original date/timezone.
            source = self.db.execute('SELECT started_at,timezone FROM bindings WHERE call_id=? AND profile=? AND caller_id=? AND started_at IS NOT NULL ORDER BY started_at LIMIT 1',
                                     (call_id, profile, caller_id)).fetchone()
            self.db.execute('UPDATE bindings SET started_at=?,timezone=? WHERE session_id=?',
                            (*(source or (time.time(), timezone)), session_id))
        return self.binding(session_id)

    def binding(self, session_id: str) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT call_id,profile,caller_id,policy,persistent,expires,active,started_at,timezone FROM bindings WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row or not row[6] or row[5] <= time.time():
            raise PermissionError("phone session is missing, expired, or revoked")
        return dict(
            call_id=row[0],
            profile=row[1],
            caller_id=row[2],
            policy=json.loads(row[3]),
            persistent=bool(row[4]),
            started_at=row[7], timezone=row[8],
        )

    def renew(self, session_id: str, ttl: float = CALLER_BINDING_TTL_SECONDS) -> None:
        with self._lock, self.db:
            self.binding(session_id)
            self.db.execute(
                "UPDATE bindings SET expires=? WHERE session_id=?",
                (time.time() + ttl, session_id),
            )

    def revoke(self, session_id: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE bindings SET active=0 WHERE session_id=?", (session_id,)
            )

    def read(self, profile: str, caller_id: str) -> str:
        with self._lock:
            row = self.db.execute(
                "SELECT note FROM notes WHERE profile=? AND caller_id=?",
                (profile, caller_id),
            ).fetchone()
        return row[0] if row else ""

    def snapshot(self, profile: str, caller_id: str) -> dict:
        with self._lock:
            row = self.db.execute("""SELECT
                COALESCE((SELECT note FROM notes WHERE profile=? AND caller_id=?), ''),
                COALESCE((SELECT revision FROM note_versions WHERE profile=? AND caller_id=?), 0),
                COALESCE((SELECT generation FROM note_versions WHERE profile=? AND caller_id=?), 0)""",
                (profile, caller_id) * 3).fetchone()
            return dict(zip(("notes", "revision", "generation"), row))

    def update_for_session(self, session_id: str, note: str, *, expected_revision=None) -> None:
        if not isinstance(note, str) or len(note) > 8000:
            raise ValueError("caller note must be text of at most 8000 characters")
        with self._lock, self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            binding = self.binding(session_id)
            if not binding["persistent"]:
                raise PermissionError(
                    "persistent caller memory is disabled for this call"
                )
            if binding['started_at'] is None:
                raise PermissionError('legacy binding lacks call provenance; start a new call')
            from .note_text import prepare_replacement
            old = self.read(binding['profile'], binding['caller_id'])
            note = prepare_replacement(old, note, binding)
            self.replace_notes(binding["profile"], binding["caller_id"], note, expected_revision=expected_revision)

    def replace_notes(self, profile: str, caller_id: str, note: str, *, expected_revision=None) -> None:
        """Storage primitive; callers must authorize the resolved caller/profile."""
        if not isinstance(note, str) or len(note) > 8000:
            raise ValueError("caller note must be text of at most 8000 characters")
        with self._lock, self.db:
            self._replace_notes(profile, caller_id, note, expected_revision=expected_revision)

    def _replace_notes(self, profile, caller_id, note, *, expected_revision=None):
        """Transaction-internal write, also used by atomic closeout commits."""
        if not isinstance(note, str) or len(note) > 8000:
            raise ValueError("caller note must be text of at most 8000 characters")
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        current = self.snapshot(profile, caller_id)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != current["revision"]):
            raise NoteConflict("notes changed; read the latest notes and merge again")
        self.db.execute(
            "INSERT INTO note_versions VALUES (?,?,?,?) ON CONFLICT(profile,caller_id) DO UPDATE SET revision=excluded.revision",
            (profile, caller_id, current["revision"] + 1, current["generation"]))
        self.db.execute(
            "INSERT INTO notes VALUES (?,?,?,?) ON CONFLICT(profile,caller_id) DO UPDATE SET note=excluded.note, updated=excluded.updated",
            (profile, caller_id, note, time.time()))

    def forget(self, profile: str, caller_id: str) -> None:
        with self._lock, self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            old = self.snapshot(profile, caller_id)
            self.db.execute("INSERT INTO note_versions VALUES (?,?,?,?) ON CONFLICT(profile,caller_id) DO UPDATE SET revision=excluded.revision,generation=excluded.generation",
                            (profile, caller_id, old["revision"] + 1, old["generation"] + 1))
            self.db.execute("DELETE FROM notes WHERE profile=? AND caller_id=?", (profile, caller_id))
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_closeouts'").fetchone():
                self.db.execute("DELETE FROM memory_closeouts WHERE profile=? AND caller_id=?", (profile, caller_id))

    def close(self):
        self.db.close()

    def managed(self, root_id):
        with self._lock:
            row = self.db.execute("SELECT conversation_id,profile,caller_id,active FROM managed_sessions WHERE root_id=?", (root_id,)).fetchone()
        return dict(conversation_id=row[0], profile=row[1], caller_id=row[2], active=bool(row[3])) if row else None

    def manage(self, conversation_id, binding_id):
        binding = self.binding(binding_id)
        with self._lock, self.db:
            row = self.db.execute("SELECT root_id FROM managed_sessions WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row:
                self.check_managed(row[0], binding_id)
                return row[0]
            root = "hfp-chat-" + secrets.token_hex(16)
            self.db.execute("INSERT INTO managed_sessions VALUES (?,?,?,?,1)",
                            (root, conversation_id, binding["profile"], binding["caller_id"]))
            return root

    def check_managed(self, root_id, binding_id, *, active=True):
        binding = self.binding(binding_id)
        session = self.managed(root_id)
        if not session or (active and not session["active"]) or any(
            session[key] != binding[key] for key in ("profile", "caller_id")
        ):
            raise PermissionError("conversation is not owned by this caller")
        return binding

    def retire_managed(self, root_id):
        with self._lock, self.db:
            self.db.execute("UPDATE managed_sessions SET active=0 WHERE root_id=?", (root_id,))

    def bind_run(self, run_id, binding_id, root_id):
        with self._lock, self.db:
            self.check_managed(root_id, binding_id)
            old = self.db.execute("SELECT binding_id,root_id FROM run_bindings WHERE run_id=?", (run_id,)).fetchone()
            if old and old != (binding_id, root_id):
                raise PermissionError("run authority cannot be replaced")
            self.db.execute("INSERT OR IGNORE INTO run_bindings VALUES (?,?,?)", (run_id, binding_id, root_id))

    def run_binding(self, run_id):
        with self._lock:
            row = self.db.execute("SELECT binding_id,root_id FROM run_bindings WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise PermissionError("native run has no phone authority")
        self.tasks.authority(run_id)
        binding = self.check_managed(row[1], row[0])
        return {**binding, "binding_id": row[0], "root_id": row[1], "run_id": run_id}
