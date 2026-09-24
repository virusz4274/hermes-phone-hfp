from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.note_text import clean_fact, prepare_replacement


STAMP = datetime(2026, 9, 21, 23, 59, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
BINDING = dict(call_id='actual-call', started_at=STAMP, timezone='Asia/Kolkata')
OLD = '- [2026-09-20T12:00:00+05:30 Asia/Kolkata; call older-call] Caller-reported fact: Delivery ordered.'


def test_new_lines_cannot_supply_provenance_and_old_lines_remain_unchanged():
    forged = '- [2099-01-01T00:00:00 UTC; call invented] Caller-reported fact: Caller-reported fact: RAM arrives tomorrow.'
    saved = prepare_replacement(OLD, OLD + '\n' + forged + '\n' + forged, BINDING)
    assert saved.splitlines()[0] == OLD
    assert len(saved.splitlines()) == 2
    assert 'invented' not in saved and '2099' not in saved
    assert saved.splitlines()[1] == '- [2026-09-21T23:59:00+05:30 Asia/Kolkata; call actual-call] Caller note update: RAM arrives 2026-09-22.'
    assert prepare_replacement(saved, saved, {**BINDING, 'call_id': 'next-call'}) == saved


def test_renderer_labels_and_dates_are_not_repeated_or_confused():
    raw = 'Caller-reported fact: Caller-reported fact: Delivery. Due: 2026-09-22. Due: 2026-09-22.'
    assert clean_fact(raw, due_date='2026-09-22') == 'Delivery.'
    assert clean_fact(raw) == 'Delivery. Due: 2026-09-22.'
    assert clean_fact(raw, due_date='2026-09-23') == 'Delivery. Due: 2026-09-22.'
    assert clean_fact('Delivery. Due: 2026-09-21. Due: 2026-09-22.') == 'Delivery. Due: 2026-09-21. Due: 2026-09-22.'


def test_child_request_inherits_call_date_and_timezone_across_midnight(tmp_path, monkeypatch):
    monkeypatch.setattr('hfp_mcp.caller_context.time.time', lambda: STAMP)
    store = CallerStore(tmp_path / 'callers.sqlite3')
    args = dict(call_id='actual-call', profile='default', number='+919876543210', policy={'admin': True}, ttl=3600)
    try:
        root = store.bind(session_id='root', timezone='Asia/Kolkata', **args)
        monkeypatch.setattr('hfp_mcp.caller_context.time.time', lambda: STAMP + 120)
        child = store.bind(session_id='child', **args)
        assert child['started_at'] == root['started_at'] == STAMP
        assert child['timezone'] == root['timezone'] == 'Asia/Kolkata'
        store.update_for_session('child', 'RAM arrives tomorrow.', expected_revision=0)
        assert 'RAM arrives 2026-09-22.' in store.read('default', root['caller_id'])
        assert '2026-09-21T23:59:00' in store.read('default', root['caller_id'])
        with store.db:
            store.db.execute("UPDATE bindings SET started_at=NULL WHERE session_id='child'")
        with pytest.raises(PermissionError, match='legacy binding'):
            store.update_for_session('child', 'Cannot invent the original date.')
    finally:
        store.close()


def test_legacy_database_migration_keeps_notes_and_blocks_undated_lease(tmp_path):
    import json
    import sqlite3
    import time
    path = tmp_path / 'callers.sqlite3'
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.executescript('''
        CREATE TABLE notes (profile TEXT NOT NULL, caller_id TEXT NOT NULL,
            note TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(profile,caller_id));
        CREATE TABLE bindings (session_id TEXT PRIMARY KEY, call_id TEXT NOT NULL,
            profile TEXT NOT NULL, caller_id TEXT NOT NULL, policy TEXT NOT NULL,
            persistent INTEGER NOT NULL, expires REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    ''')
    db.execute('INSERT INTO notes VALUES (?,?,?,?)', ('default', 'caller', OLD, time.time()))
    db.execute('INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?)',
               ('legacy', 'old-call', 'default', 'caller', json.dumps({'admin': True}), 1, time.time()+60, 1))
    db.commit(); db.close()
    for _ in range(2):
        store = CallerStore(path)
        try:
            assert store.read('default', 'caller') == OLD
            assert store.binding('legacy')['started_at'] is None
            with pytest.raises(PermissionError, match='legacy binding'):
                store.update_for_session('legacy', 'Do not invent a date.')
            assert store.read('default', 'caller') == OLD
        finally:
            store.close()
