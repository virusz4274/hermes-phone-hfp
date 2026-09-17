"""Narrow adapter to native Hermes storage/compaction; no dialogue mirroring."""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def native_db(store, *, read_only=False):
    from hermes_state import SessionDB
    db = SessionDB(store.path.parent.parent / "state.db", read_only=read_only)
    try:
        yield db
    finally:
        db.close()


def managed_root(store, session_id):
    if store.managed(session_id):
        return session_id
    with native_db(store, read_only=True) as db:
        seen = set()
        while session_id and session_id not in seen:
            seen.add(session_id)
            row = db.get_session(session_id)
            if not row:
                return None
            session_id = row.get("parent_session_id")
            if session_id and store.managed(session_id):
                return session_id
    return None


def create(store, root, profile):
    with native_db(store) as db:
        if not db.get_session(root):
            db.create_session(root, "api_server", profile_name=profile)
        return db.resolve_resume_session_id(root)


def lineage(db, root):
    # Native compaction forms a single continuation chain; branch sessions are
    # deliberately excluded. Resolve each live tip through native semantics.
    tip = db.resolve_resume_session_id(root)
    chain, current = [], tip
    while current:
        chain.append(current)
        if current == root:
            return chain
        row = db.get_session(current)
        current = row.get("parent_session_id") if row else None
    raise PermissionError("invalid native compression lineage")


def remove(store, root):
    with native_db(store) as db:
        if not db.get_session(root):
            return
        for sid in lineage(db, root):
            db.delete_session(sid)


def compact(store, root, adapter, profile):
    from .hermes_compat import native_readiness
    if not native_readiness(adapter)['compaction']:
        return {'status': 'error', 'message': 'Native compaction is unavailable in this Hermes runtime; see docs/compatibility.md.'}
    with adapter._profile_scope(profile), native_db(store) as db:
        tip = db.resolve_resume_session_id(root)
        messages = db.get_messages_as_conversation(tip)
        if len(messages) < 4:
            return {"status": "ok", "message": "Hermes has too little task history to compact.", "session_id": tip}
        agent = adapter._create_agent(session_id=tip)
        agent._end_session_on_close = False
        try:
            if getattr(agent, "api_mode", None) == "codex_app_server":
                return {"status": "error", "message": "This runtime keeps compaction in its live native thread; temporary-agent compaction is unavailable."}
            # Mark the loaded prefix as already durable, as native turn setup
            # does. The native compressor atomically persists rotation/in-place
            # changes, including summaries and lineage, under its own lease.
            agent._persist_user_message_idx = len(messages)
            agent._compress_context(messages, "", force=True)
            durable_tip = db.resolve_resume_session_id(root)
            changed = durable_tip != tip or db.get_messages_as_conversation(durable_tip) != messages
            return {"status": "ok", "session_id": durable_tip,
                    "message": "Hermes task context compacted; phone dialogue is retained." if changed else
                    "Native compaction made no change. Phone dialogue is retained."}
        finally:
            agent.close()
