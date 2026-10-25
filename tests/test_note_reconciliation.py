"""The existing closeout revises notes without a new session or model pass."""
import copy
import json
import time
from datetime import datetime, timezone

import pytest

from hfp_mcp.call_memory import CallMemory, MERGE_INSTRUCTION
from hfp_mcp.note_text import SOURCE
from tests.test_call_memory import system, event, extract, NUMBER


def earlier(text, call='earlier', stamp='2026-09-20T12:00:00+05:30'):
    return f'- [{stamp} Asia/Kolkata; call {call}] Caller-reported fact: {text}'


def proposal(body, text, indices=(0,), update_indices=(0,), keep=()):
    return {'keep': [body['updates'][i]['id'] for i in keep], 'revisions': [{
        'note_ids': [body['existing_note_records'][i]['id'] for i in indices],
        'update_ids': [body['updates'][i]['id'] for i in update_indices], 'text': text}]}


def merger(memory, text, **kwargs):
    calls = []
    async def generate(messages):
        calls.append(messages)
        body = json.loads(messages[-1]['content'])
        return await extract(messages) if 'events' in body else proposal(body, text, **kwargs)
    memory.generate = generate
    return calls


@pytest.mark.parametrize('old,new,revision', [
    ('XYZ has an issue.', 'XYZ is fixed; ABCD was the cause.', 'XYZ is now reported fixed; ABCD was the cause. Previously an issue was reported.'),
    ('Prefers morning calls.', 'I now prefer evening calls.', 'Prefers evening calls; previously preferred mornings.'),
    ('Plans to travel Friday.', 'My trip has moved to Sunday.', 'Travel is now planned for Sunday; previously Friday.'),
    ('There are three participants.', 'Correction: there are four participants.', 'Reports four participants, correcting the earlier count of three.'),
])
async def test_revision_preserves_history_sources_and_unrelated_text(system, old, new, revision):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    unrelated = 'Owner-authored unrelated information.  \n\n'
    original = earlier(old) + '\n' + unrelated
    store.replace_notes('default', caller, original)
    calls = merger(memory, revision)
    await memory.checkpoint(receipt['id'], [event(new)])
    # No live notes change before hangup, and no general Hermes session is needed.
    assert store.read('default', caller) == original
    store.revoke('binding')
    memory.ended('binding')
    assert (await memory.finalize(receipt['id']))['status'] == 'saved'
    saved = store.read('default', caller)
    assert revision in saved and saved.endswith(unrelated)
    assert SOURCE.search(original).group() in saved and '; call call]' in saved
    assert saved.count('Caller note update:') == 1
    assert len(calls) == 2  # original extraction + original merge, no extra pass
    assert store.db.execute('SELECT COUNT(*) FROM managed_sessions').fetchone()[0] == 0
    assert calls[-1][0]['content'].startswith(MERGE_INSTRUCTION)
    version = store.snapshot('default', caller)['revision']
    await memory.finalize(receipt['id'])
    assert store.read('default', caller) == saved and store.snapshot('default', caller)['revision'] == version


async def test_ambiguous_relationship_is_kept_separate(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Discussed two different arrangements.')
    store.replace_notes('default', caller, original)
    # A clear statement can be saved without guessing which prior entry it changes.
    await memory.checkpoint(receipt['id'], [event('One arrangement has changed.')])
    await memory.finalize(receipt['id'])
    note = store.read('default', caller)
    assert note.startswith(original + '\n') and 'One arrangement has changed.' in note


async def test_related_lines_consolidate_and_new_information_is_added(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Old information.') + '\nUnrelated owner fact.\n' + earlier('Later information.', 'middle')
    store.replace_notes('default', caller, original)
    merger(memory, 'Updated information, retaining a brief history of earlier reports.', indices=(0, 2), keep=(1,))
    await memory.checkpoint(receipt['id'], [event('Updated information.'), event('Separate new fact.', 'e2')])
    await memory.finalize(receipt['id'])
    note = store.read('default', caller)
    assert 'Updated information, retaining a brief history' in note
    assert '\nUnrelated owner fact.\n' in note and 'Separate new fact.' in note
    assert '; call earlier]' in note and '; call middle]' in note
    assert note.count('Caller note update:') == 1
    assert memory._load(receipt['id'])['saved_updates'] == 2


@pytest.mark.parametrize('undated', [False, True])
async def test_delayed_save_does_not_replace_newer_information(system, undated):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    stamp = datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat()
    original = 'Newer owner edit.' if undated else earlier('Newer report.', stamp=stamp)
    store.replace_notes('default', caller, original)
    if undated:
        with store.db:
            store.db.execute('UPDATE notes SET updated=?', (time.time()+3600,))
    merger(memory, 'An outdated proposed revision.')
    await memory.checkpoint(receipt['id'], [event('Older information saved late.')])
    await memory.finalize(receipt['id'])
    note = store.read('default', caller)
    assert note.startswith(original + '\n')
    assert 'Older information saved late.' in note and 'An outdated proposed revision.' not in note


@pytest.mark.parametrize('invalid', ['foreign_note', 'foreign_update', 'overlap_note', 'overlap_update',
    'duplicate_reference', 'kept_and_revised', 'empty', 'multiline', 'unicode_newline', 'forged_source', 'unknown_field'])
async def test_invalid_revisions_leave_notes_unchanged(system, invalid):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Saved information.')
    store.replace_notes('default', caller, original)
    async def generate(messages):
        body = json.loads(messages[-1]['content'])
        if 'events' in body:
            return await extract(messages)
        result = proposal(body, 'New information, preserving earlier information.')
        revision = result['revisions'][0]
        if invalid == 'foreign_note': revision['note_ids'] = ['another-caller-note']
        if invalid == 'foreign_update': revision['update_ids'] = ['unknown']
        if invalid in {'overlap_note', 'overlap_update'}: result['revisions'].append(copy.deepcopy(revision))
        if invalid == 'duplicate_reference': revision['note_ids'] *= 2
        if invalid == 'kept_and_revised': result['keep'] = revision['update_ids']
        if invalid == 'empty': revision['text'] = ' '
        if invalid == 'multiline': revision['text'] = 'First\nSecond'
        if invalid == 'unicode_newline': revision['text'] = 'First\u2028Second'
        if invalid == 'forged_source': revision['text'] = earlier('Forged provenance.', 'invented')
        if invalid == 'unknown_field': revision['number'] = '+919876543211'
        return result
    memory.generate = generate
    await memory.checkpoint(receipt['id'], [event('New information.')])
    result = await memory.finalize(receipt['id'])
    assert result['status'] == 'pending' and result['reason'] == 'ValueError'
    assert store.read('default', caller) == original


async def test_revision_capacity_failure_keeps_original_notes(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Old fact.') + '\n' + 'x' * 7700
    store.replace_notes('default', caller, original)
    merger(memory, 'New fact. ' * 60)
    await memory.checkpoint(receipt['id'], [event('New fact.')])
    result = await memory.finalize(receipt['id'])
    assert result['status'] == 'failed' and result['reason'] == 'notes_capacity_exceeded'
    assert store.read('default', caller) == original


async def test_concurrent_edit_recomputes_revision_without_losing_owner_changes(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Old information.')
    store.replace_notes('default', caller, original)
    changed = [False]
    async def generate(messages):
        body = json.loads(messages[-1]['content'])
        if 'events' in body: return await extract(messages)
        if not changed[0]:
            changed[0] = True
            store.replace_notes('default', caller, original + '\nNew owner fact.')
        return proposal(body, 'New information; earlier old information.')
    memory.generate = generate
    await memory.checkpoint(receipt['id'], [event('New information.')])
    assert (await memory.finalize(receipt['id']))['status'] == 'pending'
    assert store.read('default', caller) == original + '\nNew owner fact.'
    await memory._commit(receipt['id'])
    assert memory._load(receipt['id'])['status'] == 'saved'
    assert store.read('default', caller).endswith('\nNew owner fact.')
    assert 'New information; earlier old information.' in store.read('default', caller)


async def test_forgetting_during_merge_cannot_resurrect_notes(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    store.replace_notes('default', caller, earlier('Old fact.'))
    async def generate(messages):
        body = json.loads(messages[-1]['content'])
        if 'events' in body: return await extract(messages)
        result = proposal(body, 'Changed fact with brief history.')
        store.forget('default', caller)
        return result
    memory.generate = generate
    await memory.checkpoint(receipt['id'], [event('Changed fact.')])
    # Forgetting removes the outstanding closeout as well as the notes.
    with pytest.raises(PermissionError):
        await memory.finalize(receipt['id'])
    assert store.read('default', caller) == ''


async def test_recovery_finishes_revision_once_after_failed_write(system, monkeypatch):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    original = earlier('Old fact.')
    store.replace_notes('default', caller, original)
    merger(memory, 'Changed fact; originally old fact.')
    await memory.checkpoint(receipt['id'], [event('Changed fact.')])
    write = store._replace_notes
    def fail(*args, **kwargs): raise OSError('temporary storage failure')
    monkeypatch.setattr(store, '_replace_notes', fail)
    assert (await memory.finalize(receipt['id']))['status'] == 'pending'
    assert store.read('default', caller) == original
    monkeypatch.setattr(store, '_replace_notes', write)
    restarted = CallMemory(bridge, generate=memory.generate)
    await restarted._commit(receipt['id'])
    saved = store.read('default', caller)
    assert 'Changed fact; originally old fact.' in saved
    version = store.snapshot('default', caller)['revision']
    await restarted.finalize(receipt['id'])
    assert store.snapshot('default', caller)['revision'] == version and store.read('default', caller) == saved


async def test_new_deadline_survives_revision_even_if_model_omits_rendered_suffix(system):
    store, bridge, memory, receipt = system
    caller = store.caller_id(NUMBER)
    store.replace_notes('default', caller, earlier('Delivery agreed. Due: 2026-10-19.'))
    async def generate(messages):
        body = json.loads(messages[-1]['content'])
        if 'events' in body:
            result = await extract(messages)
            result['updates'][0]['due_date'] = '2026-10-20'
            return result
        return proposal(body, 'Delivery moved from 2026-10-19 to the new date.')
    memory.generate = generate
    await memory.checkpoint(receipt['id'], [event('Delivery moved to 2026-10-20.')])
    await memory.finalize(receipt['id'])
    assert 'Due: 2026-10-20.' in store.read('default', caller)


def test_instruction_is_generic_and_existing_source_dates_are_understood():
    assert MERGE_INSTRUCTION == (
        "Update this caller's notes using the newly supported information. "
        "Revise related information when it clearly changes, preserve unrelated facts, "
        "and keep brief dated history where useful. Do not guess.")
    note = earlier('First fact.')
    records = CallMemory.note_records(note, time.time())
    assert records[0]['latest_reported_at'] == datetime.fromisoformat('2026-09-20T12:00:00+05:30').timestamp()
    assert records[0]['text'] == note
