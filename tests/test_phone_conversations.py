import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from hfp_mcp.contracts import RequestLedger
from hfp_mcp.conversations import ConversationStore
from hfp_mcp.phone_controller import PhoneController
from hfp_mcp.phone_conversation import PhoneConversation
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


def event(conv, text, rid, *, age=0):
    return {"session_id": "call-" + conv["id"], "conversation_id": conv["id"],
            "provider": "gemini", "direction": "input", "text": text,
            "timestamp": time.time()-age, "event_id": rid}


def test_continuity_isolation_retention_and_idempotent_transcripts(tmp_path):
    path = tmp_path / "calls.db"
    ledger = RequestLedger(path)
    store = ConversationStore(ledger)
    a = store.open("default", "alice")
    b = store.open("default", "bob")
    other = store.open("guest", "alice")
    for conv, text in [(a, "biriyani"), (b, "bob secret"), (other, "other profile")]:
        ledger.append_transcript(event(conv, text, conv["id"], age=60*86400))
    ledger.append_transcript(event(a, "biriyani", a["id"]))
    ledger.prune()
    assert [m["text"] for m in store.recall(a, archived=True)["messages"]] == ["biriyani"]
    ledger.close()
    ledger = RequestLedger(path)
    store = ConversationStore(ledger)
    assert store.open("default", "alice")["id"] == a["id"]
    assert len(store.recall(a)["messages"]) == 1
    store.archive(a)
    fresh = store.open("default", "alice")
    assert fresh["id"] != a["id"]
    assert not store.recall(fresh)["messages"]
    assert store.recall(fresh, archived=True)["messages"][0]["text"] == "biriyani"
    store.delete(fresh)
    assert not store.recall(fresh, archived=True)["messages"]
    assert store.recall(b)["messages"][0]["text"] == "bob secret"
    ledger.close()


def test_old_unlinked_transcripts_are_not_imported_and_recall_is_bounded(tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db")
    store = ConversationStore(ledger)
    a = store.open("default", "a")
    old = event(a, "old test", "old")
    old.pop("conversation_id")
    ledger.append_transcript(old)
    assert not store.recall(a)["messages"]
    for n in range(50):
        ledger.append_transcript(event(a, str(n) + "x"*1000, str(n)))
    recent = store.recall(a)
    assert len(recent["messages"]) <= 40
    assert sum(len(m["text"]) for m in recent["messages"]) <= 24000
    assert recent["has_more"]
    older = store.recall(a, before_id=recent["before_id"])
    assert not {m["id"] for m in recent["messages"]} & {m["id"] for m in older["messages"]}
    ledger.close()


def test_anonymous_or_memory_disabled_calls_do_not_resume(tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db")
    store = ConversationStore(ledger)
    a = store.open("guest", "a", False)
    ledger.append_transcript(event(a, "private", "1"))
    b = store.open("guest", "a", False)
    assert not store.recall(b, archived=True)["messages"]
    store.archive(a)
    with ledger._db:
        ledger._db.execute("UPDATE phone_conversations SET archived_at=? WHERE id=?", (time.time()-31*86400, a["id"]))
    ledger.prune()
    assert not store.recall(a)["messages"]
    ledger.close()


class NativeAPI:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.revoked = []
        self.requests = []
        self.sessions = []
        self.tasks = {}
    async def create_session(self, binding, conversation):
        root = "hfp-chat-" + conversation
        self.sessions.append(root)
        return {"session_id": root}
    async def revoke(self, sid):
        self.revoked.append(sid)
    async def phone_tasks(self, binding, conversation, *, submit=False, **args):
        if submit:
            if self.tasks and not self.finish.is_set():
                return {"status":"busy", "message":"A task is active; another was not started."}
            self.requests.append({"binding_id":binding, "conversation_id":conversation, **args})
            row = {"task_id":args["request_id"], "status":"accepted", "message":"Accepted", "run_id":"run-test"}
            self.tasks[row["task_id"]] = row
            self.started.set()
            return row
        if args.get("action") == "cancel":
            self.tasks[args["task_id"]]["status"] = "cancelled"
            return {"status":"cancelled"}
        if args.get("action") == "steer":
            return {"status":"pending", "message":"Correction accepted"}
        if self.finish.is_set():
            for row in self.tasks.values():
                if row["status"] == "accepted": row.update(status="completed",message="RAM is 4 GB")
        return {"status":"ok", "tasks":[dict(t) for t in self.tasks.values()]}
    async def session_control(self, binding, session, action):
        self.tasks.clear()
        return {"status": "ok", "message": action}


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate(): await asyncio.sleep(.01)


async def engine(tmp_path):
    data = routing_data()
    data["numbers"]["+919876543210"]["continuity"] = True
    ledger = RequestLedger(tmp_path / "calls.db")
    c = PhoneController(RoutingConfig.parse(data), snapshot=lambda: {"call": {"id": "call", "state": "active"}},
        answer=None, end=None, make_voice=None, ledger=ledger)
    c.call_id, c._number = "call", "+919876543210"
    c.route = c.config.resolve(c._number)[0]
    c.binding = {"session_id": "hfp-main", "caller_id": "alice", "persistent": True}
    c.api = NativeAPI()
    notified = []
    async def notify(result, request_id):
        notified.append(result)
    c.voice = SimpleNamespace(notify=notify)
    e = c.conversation = PhoneConversation(c, ConversationStore(ledger))
    await e.start()
    return c, e, notified


async def test_background_task_acceptance_context_and_explicit_cancellation(tmp_path):
    c, e, notified = await engine(tmp_path)
    try:
        e.capture({**event(e.conversation, "I like blue logos", "unused"), "session_id": "call"})
        result = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "make the logo"}, request_id="one"))
        assert result["status"] == "pending"
        assert e.busy()
        assert not notified
        handed = c.api.requests[0]
        assert handed["conversation_id"] == e.conversation["id"]
        assert handed["binding_id"] == "hfp-main"
        assert "history" not in handed
        assert "I like blue logos" in handed["text"]
        again = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "another"}, request_id="two"))
        assert "not started" in again["message"]
        assert len(c.api.requests) == 1
        assert (await e.task_control({"action": "steer", "text": "use green"}))["status"] == "pending"
        c.api.finish.set()
        await wait_until(lambda: bool(notified))
        assert notified[-1]["status"] == "completed"
    finally:
        await e.close()
        c.ledger.close()


async def test_new_chat_requires_subsequent_input_and_fences_old_transcripts(tmp_path):
    c, e, _ = await engine(tmp_path)
    try:
        old = e.conversation["id"]
        answer = await e.session_control({"action": "new"})
        confirm = {"action": "confirm", "confirmation_token": answer["confirmation_token"]}
        assert (await e.session_control(confirm))["status"] == "error"
        e.capture({**event(e.conversation, "yes start fresh", "1"), "session_id": "call"})
        result = await e.session_control(confirm)
        assert result["status"] == "ok"
        assert e.conversation["id"] != old
        assert result["_refresh_generation"] == 1
        assert e.refreshing
        e.voice_ready(1)
        e.capture({**event(e.conversation, "late old model speech", "2"), "metadata": {"conversation_generation": 0}})
        await e.flush()
        assert not e.store.recall(e.conversation)["messages"]
        assert (await e.session_control(confirm))["status"] == "error"
        answer = await e.session_control({"action": "delete"})
        e.capture({**event(e.conversation, "yes delete", "3"), "metadata": {"conversation_generation": 1}})
        assert (await e.session_control({"action": "confirm", "confirmation_token": answer["confirmation_token"]}))["status"] == "ok"
        assert not e.store.recall(e.conversation, archived=True)["messages"]
    finally:
        await e.close()
        c.ledger.close()


async def test_hangup_cancels_work_and_drops_late_notifications(tmp_path):
    c, e, notified = await engine(tmp_path)
    await e.submit("read RAM", SimpleNamespace(request_id="one"))
    await e.close()
    await e.notify({"status": "completed", "message": "late"}, 0, "one")
    assert not notified
    # Closing the voice observer does not cancel gateway-owned continuation.
    assert e.observer.done()
    c.ledger.close()


async def test_cancelled_admission_waiter_does_not_cancel_native_task(tmp_path):
    c, e, notified = await engine(tmp_path)
    gate = asyncio.Event()
    original = c.api.phone_tasks
    async def delayed(*args, **kwargs):
        if kwargs.get("submit"): await gate.wait()
        return await original(*args, **kwargs)
    c.api.phone_tasks = delayed
    waiter = asyncio.create_task(e.submit("read RAM", SimpleNamespace(request_id="one")))
    try:
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        gate.set()
        await asyncio.wait_for(c.api.started.wait(), 1)
        c.api.finish.set()
        await wait_until(lambda: bool(notified))
        assert notified[-1]["status"] == "completed"
    finally:
        await e.close()
        c.ledger.close()


async def test_incomplete_delete_keeps_retry_references_but_selects_fresh_chat(tmp_path):
    c, e, _ = await engine(tmp_path)
    try:
        old = dict(e.conversation)
        answer = await e.session_control({"action": "delete"})
        e.input_serial += 1
        async def fail(*args):
            raise RuntimeError("native unavailable")
        c.api.session_control = fail
        result = await e.session_control({"action":"confirm", "confirmation_token":answer["confirmation_token"]})
        assert result["status"] == "error"
        assert "old history may remain" in result["message"]
        assert e.conversation["id"] != old["id"]
        assert old["hermes_session"] in e.store.sessions(e.conversation)
        assert e.generation == 1
    finally:
        await e.close()
        c.ledger.close()


async def test_function_boundary_finalizes_spoken_confirmation(tmp_path):
    from hfp_mcp.gemini_live import GeminiLiveManager
    c, e, _ = await engine(tmp_path)
    async def ok(*_): return {"ok": True}
    manager = GeminiLiveManager(ensure_stream=ok, clear_playback=ok, hangup=ok,
        full_transcripts_enabled=True, transcript_sink=e.capture,
        allowed_tools={"phone_session"})
    manager._session_id = "call"
    async def boundary(identity):
        await manager._handle_tool_calls(SimpleNamespace(tool_call=SimpleNamespace(
            function_calls=[SimpleNamespace(id=identity, name="phone_session", args={"action":"status"})])))
    try:
        manager.append_transcript_fragment("input", "Start a new chat", final=False)
        await boundary("first")
        pending = await e.session_control({"action":"new"})
        assert e.input_serial == 1
        rejected = await e.session_control({"action":"confirm", "confirmation_token":pending["confirmation_token"]})
        assert rejected["status"] == "error"
        manager.append_transcript_fragment("input", "Yes, start fresh", final=False)
        await boundary("second")
        assert e.input_serial == 2
        result = await e.session_control({"action":"confirm", "confirmation_token":pending["confirmation_token"]})
        assert result["status"] == "ok"
    finally:
        await e.close(); e.store.ledger.close()


async def test_storage_failure_keeps_voice_context_available(tmp_path, monkeypatch):
    c, e, _ = await engine(tmp_path)
    def fail(_): raise OSError("disk full")
    monkeypatch.setattr(e.store.ledger, "append_transcript", fail)
    try:
        e.capture({"direction":"input", "text":"hi", "session_id":"call", "provider":"gemini", "timestamp":time.time()})
        assert "temporarily unavailable" in await e.context()
        assert c.status["transcript_storage_error"] == "OSError"
        assert not e.closed
    finally:
        await e.close(); e.store.ledger.close()


def test_multilingual_recall_fits_tool_response_and_can_read_every_character(tmp_path):
    from hfp_mcp.gemini_live import _structured_tool_outcome
    ledger = RequestLedger(tmp_path / 'calls.db')
    store = ConversationStore(ledger)
    a, b = store.open('default','alice'), store.open('default','bob')
    original = 'എന്റെ സിസ്റ്റത്തിന്റെ റാം യൂസേജ് നോക്കാമോ? ' * 1000
    ledger.append_transcript(event(a, original, 'long'))
    messages = store.recall(a, byte_limit=11000)['messages']
    text = messages[0]['text']
    sid = messages[0]['id']
    while messages[0]['truncated']:
        outcome = _structured_tool_outcome({'status':'ok','messages':messages}, speak_to_caller=True)
        assert outcome['messages']
        messages = store.recall(a, byte_limit=11000, message_id=sid, offset=messages[0]['next_offset'])['messages']
        text += messages[0]['text']
    assert text == original
    assert not store.recall(b, message_id=sid)['messages']
    ledger.close()


async def test_compaction_is_background_and_preserves_ongoing_dialogue(tmp_path):
    c, e, notified = await engine(tmp_path)
    finish, refreshes = asyncio.Event(), []
    async def compact(*_):
        await finish.wait()
        return {"status":"ok", "message":"Native task context compacted."}
    async def refresh(context, generation):
        refreshes.append(context)
        e.voice_ready(generation)
    c.api.session_control = compact
    c.voice.refresh = refresh
    try:
        result = await asyncio.wait_for(e.session_control({"action":"compact"}), 0.1)
        assert result['status'] == 'pending'
        assert e.busy()
        e.capture({"direction":"input", "text":"Keep talking during compaction", "session_id":"call", "provider":"gemini", "timestamp":time.time()})
        await e.flush()
        assert 'Keep talking' in (await e.context())
        finish.set()
        await e.task
        assert refreshes and 'Keep talking' in refreshes[0]
        assert notified[-1]['status'] == 'completed'
        assert not e.refreshing
    finally:
        await e.close(); e.store.ledger.close()


async def test_interrupted_result_announcement_does_not_keep_task_busy(tmp_path):
    c,e,_ = await engine(tmp_path)
    async def interrupted(*args): raise RuntimeError('caller speaking')
    c.voice.notify = interrupted
    try:
        await e.submit('check RAM',SimpleNamespace(request_id='one'))
        c.api.finish.set()
        await wait_until(lambda: e.tasks and e.tasks[0]['status'] == 'completed')
        assert not e.busy()
        status = await e.task_control({'action':'status'})
        assert status['tasks'][0]['status'] == 'completed'
        assert 'RAM' in status['tasks'][0]['message']
    finally:
        await e.close(); c.ledger.close()


async def test_old_status_response_cannot_enter_new_conversation(tmp_path):
    c,e,_ = await engine(tmp_path)
    ready, finish = asyncio.Event(), asyncio.Event()
    async def delayed(*args,**kwargs):
        ready.set(); await finish.wait()
        return {'tasks':[{'task_id':'old','status':'completed','message':'old result'}]}
    e.observer.cancel()
    await asyncio.gather(e.observer,return_exceptions=True)
    c.api.phone_tasks = delayed
    lookup = asyncio.create_task(e.sync_tasks())
    try:
        await ready.wait()
        e.generation += 1
        e.conversation = {**e.conversation,'id':'b'*32}
        finish.set()
        await lookup
        assert e.tasks == []
    finally:
        await e.close(); c.ledger.close()
