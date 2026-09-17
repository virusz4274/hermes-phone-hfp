"""Phone dialogue in the existing ledger; Hermes owns its separate task history."""
from __future__ import annotations

import json
import time
import uuid


def prepare_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS phone_conversations (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, caller_id TEXT NOT NULL,
            persistent INTEGER NOT NULL, hermes_session TEXT,
            created_at REAL NOT NULL, archived_at REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS phone_active_conversation
            ON phone_conversations(profile,caller_id)
            WHERE archived_at IS NULL AND persistent=1;
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(call_transcript)")}
    if "conversation_id" not in columns:
        db.execute("ALTER TABLE call_transcript ADD COLUMN conversation_id TEXT")
    if "event_id" not in columns:
        db.execute("ALTER TABLE call_transcript ADD COLUMN event_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS transcript_conversation ON call_transcript(conversation_id,id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS transcript_event ON call_transcript(event_id) WHERE event_id IS NOT NULL")


class ConversationStore:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db, self.lock = ledger._db, ledger._lock

    def open(self, profile, caller_id, persistent=True):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT id,hermes_session FROM phone_conversations WHERE profile=? AND caller_id=? "
                "AND persistent=1 AND archived_at IS NULL", (profile, caller_id),
            ).fetchone() if persistent else None
            if not row:
                row = (uuid.uuid4().hex, None)
                self.db.execute("INSERT INTO phone_conversations VALUES (?,?,?,?,?,?,NULL)",
                                (row[0], profile, caller_id, int(persistent), None, time.time()))
            return {"id": row[0], "hermes_session": row[1], "profile": profile,
                    "caller_id": caller_id, "persistent": persistent}

    def link(self, conversation, session_id):
        with self.lock, self.db:
            self.db.execute("UPDATE phone_conversations SET hermes_session=? WHERE id=? AND profile=? AND caller_id=?",
                            (session_id, conversation["id"], conversation["profile"], conversation["caller_id"]))
        conversation["hermes_session"] = session_id

    def archive(self, conversation):
        with self.lock, self.db:
            self.db.execute("UPDATE phone_conversations SET archived_at=COALESCE(archived_at,?) WHERE id=? AND profile=? AND caller_id=?",
                            (time.time(), conversation["id"], conversation["profile"], conversation["caller_id"]))

    def sessions(self, conversation):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT hermes_session FROM phone_conversations WHERE profile=? AND caller_id=? AND hermes_session IS NOT NULL",
                (conversation["profile"], conversation["caller_id"]))]

    def delete(self, conversation):
        with self.lock, self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM phone_conversations WHERE profile=? AND caller_id=?",
                                                (conversation["profile"], conversation["caller_id"]))]
            for cid in ids:
                calls = [r[0] for r in self.db.execute("SELECT DISTINCT call_id FROM call_transcript WHERE conversation_id=?", (cid,))]
                self.db.execute("DELETE FROM call_transcript WHERE conversation_id=?", (cid,))
                for call in calls:
                    self.db.execute("DELETE FROM call_summary WHERE call_id=?", (call,))
                self.db.execute("DELETE FROM phone_conversations WHERE id=?", (cid,))

    def recall(self, conversation, *, query="", before_id=None, limit=40, chars=24000, archived=False, byte_limit=None, message_id=None, offset=0):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text of at most 500 characters")
        limit = min(40, max(1, int(limit)))
        args = [conversation["profile"], conversation["caller_id"]]
        where = "c.profile=? AND c.caller_id=?"
        if not archived or not conversation["persistent"]:
            where += " AND c.id=?"
            args.append(conversation["id"])
        else:
            where += " AND (c.archived_at IS NULL OR c.archived_at>=?)"
            args.append(time.time() - 30*86400)
        if query:
            where += " AND instr(lower(t.text),lower(?))>0"
            args.append(query)
        if before_id is not None:
            where += " AND t.id<?"
            args.append(int(before_id))
        offset = max(0, int(offset))
        if message_id is not None:
            where += " AND t.id=?"
            args.append(int(message_id))
        with self.lock:
            rows = self.db.execute(
                "SELECT t.id,t.direction,t.text,t.metadata_json,t.created_at FROM call_transcript t "
                "JOIN phone_conversations c ON t.conversation_id=c.id WHERE " + where +
                " ORDER BY t.id DESC LIMIT ?", (*args, limit+1),
            ).fetchall()
        selected, budget = [], chars
        for row in rows[:limit]:
            if budget <= 0:
                break
            source = row[2][offset:] if message_id is not None else row[2]
            if selected and len(source) > budget:
                # Leave this entire exchange for the next page instead of
                # advancing the cursor past a partly returned message.
                break
            text = source[:budget]
            message = {"id": row[0], "role": "user" if row[1] == "input" else "assistant",
                             "text": text, "truncated": len(text) < len(source),
                             "metadata": json.loads(row[3]), "timestamp": row[4]}
            if byte_limit and len(json.dumps(selected + [message]).encode()) > byte_limit:
                if selected:
                    break
                while len(json.dumps([message]).encode()) > byte_limit and message["text"]:
                    message["text"] = message["text"][:len(message["text"])//2]
                    message["truncated"] = True
            if message["truncated"]:
                message["next_offset"] = (offset if message_id is not None else 0) + len(message["text"])
            selected.append(message)
            budget -= len(text)
        return {"messages": list(reversed(selected)), "has_more": len(rows) > len(selected),
                "before_id": selected[-1]["id"] if selected else None,
                "scope": "retained_phone_dialogue", "untrusted_data": True}
