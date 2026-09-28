"""Owner context is scoped to the admitted call and reaches the actual provider."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hfp_mcp import server, gemini_live
from hfp_mcp.contracts import RequestLedger
from hfp_mcp.http_routes import HttpRuntime, control_routes
from hfp_mcp.phone_controller import PhoneController
from hfp_mcp.routing import RoutingConfig
from hfp_mcp.state import HFPState, CallState, ConnectionState
from tests.test_phone_routing import routing_data
from tests.test_phone_conversations import engine

NUMBER = "+919876543210"
BRIEF = "Call Arun on behalf of Mira, ask about his day, and arrange a meeting tomorrow afternoon."


def controller(state=None):
    state = state or {"connection": {"generation": 2}, "call": {
        "id": "call", "state": "active", "direction": "outgoing", "generation": 4}}
    c = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                        answer=None, end=None, make_voice=None)
    c.call_id, c._number = "call", NUMBER
    c.route = c.config.resolve(NUMBER)[0]
    c.binding = {"session_id": "hfp-call", "notes": "Arun prefers afternoons.", "persistent": True}
    return c


def stage(c, purpose=BRIEF):
    c.stage_outbound("req", NUMBER, purpose, {"connection_generation": 2, "call_generation": 3})


@pytest.mark.parametrize("mismatch", ["incoming", "connection", "generation", "number", "expired"])
def test_brief_cannot_attach_to_another_call(mismatch):
    c = controller()
    stage(c)
    state = c.snapshot()
    number = NUMBER
    if mismatch == "incoming": state["call"]["direction"] = "incoming"
    if mismatch == "connection": state["connection"]["generation"] += 1
    if mismatch == "generation": state["call"]["generation"] += 1
    if mismatch == "number": number = "+919876543211"
    if mismatch == "expired": c.pending_outbound_context["req"]["expires"] = time.monotonic() - 1
    assert c.claim_outbound("call", number) is None


async def test_brief_is_consumed_once_and_cleared_after_call():
    c = controller()
    stage(c)
    c.outbound_context = c.claim_outbound("call", NUMBER)
    assert c.call_brief() == BRIEF
    assert c.claim_outbound("call", NUMBER) is None
    assert json.loads(c.task_input("book it"))["owner_call_brief"] == BRIEF
    await c.finish()
    assert c.call_brief() == ""
    c.call_id = "later-call"
    assert c.task_input("hello") == "hello"


@pytest.mark.parametrize("purpose", ["x" * 4001, None, 42, {}, []])
async def test_invalid_purpose_rejected_before_dial(monkeypatch, purpose):
    async def fail(*args, **kwargs):
        pytest.fail("invalid purpose must not dial")
    monkeypatch.setattr(server, "_place_call", fail)
    result = await server.start_phone_call(NUMBER, "req", purpose)
    assert result["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("continuity", [False, True])
async def test_actual_gemini_configs_keep_brief_and_notes_on_refresh(tmp_path, continuity):
    if continuity:
        c, conversation, _ = await engine(tmp_path)
        c.snapshot = lambda: {"call": {"id": "call", "state": "active", "direction": "outgoing"}}
        c.binding["notes"] = "Arun prefers afternoons."
    else:
        c, conversation = controller(), None
    c.outbound_context = {"call_id": "call", "purpose": BRIEF}
    configs = []
    async def unused(*args): return {"ok": True}
    manager = gemini_live.GeminiLiveManager(ensure_stream=unused, clear_playback=unused,
        hangup=unused, context_provider=conversation.context if conversation else None,
        allowed_tools={"ask_hermes", "phone_recall"} if continuity else {"ask_hermes"})
    manager._session_id = "call"
    class Session:
        async def receive(self):
            if len(configs) == 1:
                await manager.reset_conversation(await conversation.context() if conversation else c.voice_context(), 1)
                await asyncio.Future()
            manager._stop_event.set()
            if False: yield None
    class Connection:
        async def __aenter__(self): return Session()
        async def __aexit__(self, *_): pass
    class Live:
        def connect(self, *, model, config):
            configs.append(config)
            return Connection()
    async def sender(_): await asyncio.Future()
    manager._gemini_input_sender = sender
    try:
        await asyncio.wait_for(manager._session_supervisor(SimpleNamespace(aio=SimpleNamespace(live=Live())), c.voice_context()), 2)
        assert len(configs) == 2
        for config in configs:
            assert BRIEF in config["system_instruction"]
            assert "Arun prefers afternoons." in config["system_instruction"]
            assert "Wait for the recipient to speak first" in config["system_instruction"]
        if conversation:
            result = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "book the meeting"}, request_id="booking"))
            assert result["status"] == "pending"
            assert json.loads(c.api.requests[0]["text"])["owner_call_brief"] == BRIEF
    finally:
        if conversation:
            await conversation.close()
            c.ledger.close()


@pytest.mark.parametrize("number", [NUMBER, "+919876543213"])
async def test_brief_admitted_before_atd_and_idempotent_start(tmp_path, monkeypatch, number):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    c = controller()
    c.snapshot = state.versioned_snapshot
    c.call_id = None
    ledger = RequestLedger(tmp_path / "calls.db")
    attempts = []
    class Broker:
        async def execute(self, command, **kwargs):
            attempts.append(command)
            assert c.pending_outbound_context["req"]["purpose"] == BRIEF
            state.set_call_state(CallState.DIALING)
            state.set_call_state(CallState.ACTIVE)
            c.call_id = state.call_id
            c.outbound_context = c.claim_outbound(c.call_id, number)
            c.status = {"call_id": c.call_id, "state": "ready", "voice": "gemini_live", "profile": "default"}
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_phone_controller", c)
    monkeypatch.setattr(server, "_runtime_config", None)
    monkeypatch.setattr(server, "_request_ledger", ledger)
    monkeypatch.setattr(server, "_request_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _: Broker())
    try:
        first, duplicate = await asyncio.gather(server.start_phone_call(number, "req", BRIEF), server.start_phone_call(number, "req", BRIEF))
        assert first["ok"] and duplicate == first
        assert len(attempts) == 1
        assert c.call_brief() == BRIEF
        changed = await server.start_phone_call(number, "req", "different")
        assert changed["error"]["code"] == "request_id_conflict"
        assert not c.pending_outbound_context
    finally:
        ledger.close()


async def test_failed_call_discards_pending_context(monkeypatch):
    c = controller()
    async def fail(*args, outbound):
        stage(c)
        return {"ok": False}
    monkeypatch.setattr(server, "_phone_controller", c)
    monkeypatch.setattr(server, "_place_call", fail)
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "_runtime_config", None)
    assert not (await server.start_phone_call(NUMBER, "req", BRIEF))["ok"]
    assert not c.pending_outbound_context


@pytest.mark.parametrize("purpose", [BRIEF, "മ" * 4000])
def test_http_phone_start_forwards_full_purpose(purpose):
    seen = []
    async def start(*args):
        seen.append(args)
        return {"ok": True}
    runtime = HttpRuntime(None, lambda: None, lambda: None, lambda: None, lambda: None, lambda: {}, lambda: {}, start)
    with TestClient(Starlette(routes=control_routes(None, runtime))) as client:
        response = client.post("/v1/phone/calls", json={"number": NUMBER, "request_id": "req", "purpose": purpose})
        assert response.status_code == 200
        assert seen == [(NUMBER, "req", purpose)]
        for invalid in [None, {}, 12, "x" * 4001]:
            assert client.post("/v1/phone/calls", json={"number": NUMBER, "request_id": "req", "purpose": invalid}).status_code == 400


async def test_legacy_hermes_receives_owner_objectives_and_actual_failure():
    c = controller()
    c.outbound_context = {"call_id": "call", "purpose": BRIEF}
    c.binding["caller_id"] = "arun"
    sent = []
    class API:
        run_id = "run"
        async def bind(self, *args, **kwargs): return {"session_id": "child"}
        async def revoke(self, *_): pass
        async def events(self, **kwargs):
            sent.append(kwargs["text"])
            yield {"type": "result", "status": "failed", "output": "Calendar booking was rejected"}
    c.api = API()
    result = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "book tomorrow"}, request_id="booking"))
    envelope = json.loads(sent[0])
    assert envelope["owner_call_brief"] == BRIEF
    assert envelope["caller_request"] == "book tomorrow"
    assert result == {"status": "error", "message": "Calendar booking was rejected"}


def test_reminder_needs_no_original_owner_chat_and_large_notes_fit():
    c = controller()
    reminder = "Remind Mira: design review today at 15:00 Asia/Kolkata; join the team meeting room."
    c.outbound_context = {"call_id": "call", "purpose": reminder}
    instruction = gemini_live._live_config(c.voice_context(), None, {"ask_hermes"})["system_instruction"]
    assert reminder in instruction
    assert "reminder call to the owner" in instruction
    c.outbound_context["purpose"] = "മ" * 4000
    c.binding["notes"] = "മ" * 7990 + "END-NOTES"
    instruction = gemini_live._live_config(c.voice_context(), None, {"ask_hermes"})["system_instruction"]
    assert "മ" * 4000 in instruction
    assert "END-NOTES" in instruction


async def test_cancelled_start_clears_pending_and_competing_request_cannot_remove_it(monkeypatch):
    c = controller()
    admitted = asyncio.Event()
    async def pending(*args, outbound):
        owner, request_id, brief = outbound
        owner.stage_outbound(request_id, NUMBER, brief, {"connection_generation": 2, "call_generation": 3})
        admitted.set()
        await asyncio.Future()
    monkeypatch.setattr(server, "_phone_controller", c)
    monkeypatch.setattr(server, "_place_call", pending)
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "_runtime_config", None)
    task = asyncio.create_task(server.start_phone_call(NUMBER, "req", BRIEF))
    await admitted.wait()
    c.discard_outbound("different-request")
    assert c.pending_outbound_context["req"]["purpose"] == BRIEF
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not c.pending_outbound_context
