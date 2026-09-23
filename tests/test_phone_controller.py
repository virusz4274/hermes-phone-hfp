import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from hfp_mcp.phone_controller import PhoneController
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


def renewal_controller(api, deadline=9):
    controller = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: {},
                                 answer=None, end=None, make_voice=None)
    controller.api = api
    controller.call_id = "call-1"
    controller.binding = {"session_id": "hfp-main"}
    controller._binding_deadlines["hfp-main"] = time.monotonic() + deadline
    return controller


async def test_renewal_survives_gateway_stall_longer_than_old_three_second_timeout():
    class SlowAPI:
        async def request(self, *args, **kwargs):
            await asyncio.sleep(3.2)
            return {"ok": True}

    controller = renewal_controller(SlowAPI())
    await controller._renew_binding(controller.binding)
    assert controller._binding_deadlines["hfp-main"] > time.monotonic() + 6
    assert controller.status["state"] != "failed"


async def test_renewal_retries_transport_failure_inside_existing_lease():
    class RecoveringAPI:
        calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("temporary")
            return {"ok": True}

    api = RecoveringAPI()
    controller = renewal_controller(api)
    await controller._renew_binding(controller.binding)
    assert api.calls == 2


@pytest.mark.parametrize("code", [401, 403, 409])
async def test_rejected_renewal_is_not_retried(code):
    class RejectedAPI:
        calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            response = httpx.Response(code, request=httpx.Request("POST", "http://hermes/renew"))
            response.raise_for_status()

    api = RejectedAPI()
    controller = renewal_controller(api)
    with pytest.raises(httpx.HTTPStatusError):
        await controller._renew_binding(controller.binding)
    assert api.calls == 1


async def test_renewal_cannot_wait_beyond_confirmed_expiry():
    cancelled = asyncio.Event()

    class HungAPI:
        async def request(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    controller = renewal_controller(HungAPI(), deadline=0.6)
    original = controller._binding_deadlines["hfp-main"]
    with pytest.raises(TimeoutError):
        await controller._renew_binding(controller.binding)
    assert cancelled.is_set()
    assert controller._binding_deadlines["hfp-main"] == original


async def test_heartbeat_stops_voice_and_call_when_renewal_budget_expires():
    class HungAPI:
        async def request(self, *args, **kwargs):
            await asyncio.Event().wait()

    controller = renewal_controller(HungAPI(), deadline=2.6)
    controller.voice = Voice()
    ended = []

    async def end(call_id):
        ended.append(call_id)

    controller.end = end
    await asyncio.wait_for(controller.heartbeat(), 3)
    assert ended == ["call-1"]
    assert controller.voice.stopped
    assert controller.status["reason"] == "caller_authority_lost"
    assert controller.status["authority_error"] == "TimeoutError"


def test_cancelled_request_timing_keeps_call_id_after_cleanup():
    recorded = []
    controller = renewal_controller(None)
    controller.timing_sink = lambda *args: recorded.append(args)
    controller.call_id = None
    controller.record_timing("hermes_request", {"outcome": "cancelled"}, call_id="call-1")
    assert recorded == [("call-1", "hermes_request", {"outcome": "cancelled"})]


async def test_phone_status_checks_connection_without_starting_agent_work():
    state = {"call": {"id": "call-1", "state": "active", "remote_number": "+919876543210", "remote_number_verified": True}}
    controller = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                                 answer=None, end=None, make_voice=lambda *a: Voice(), api_factory=API)
    controller.start()
    try:
        await wait_for(lambda: controller.status["state"] == "ready")
        request = SimpleNamespace(name="phone_status", arguments={}, request_id="status-1")
        result = await controller.delegate(request)
        assert result["status"] == "ok"
        assert result["profile"] == "default"
        assert result["work_pending"] is False
        assert controller.request_binding is None
        assert controller.work_started is False
        assert controller.api.actions == []
        state["call"]["state"] = "idle"
        assert (await controller.delegate(request))["status"] == "error"
    finally:
        await controller.close()


async def test_status_timeout_does_not_claim_permission_denial_or_cancel_work():
    class SlowStatusAPI:
        async def check(self):
            raise httpx.ReadTimeout("busy gateway")

    c = renewal_controller(SlowStatusAPI())
    c.snapshot = lambda: {"call": {"id": "call-1", "state": "active"}}
    c.request_binding = {"session_id": "hfp-running"}
    result = await c.delegate(SimpleNamespace(name="phone_status"))
    assert result["status"] == "pending"
    assert result["work_pending"] is True
    assert c.request_binding == {"session_id": "hfp-running"}


@pytest.mark.parametrize("admin", [True, False])
def test_voice_context_derives_access_from_route_not_caller_notes(admin):
    data = routing_data()
    data["numbers"]["+919876543210"]["policy"] = "owner" if admin else "guest"
    c = renewal_controller(None)
    c.config = RoutingConfig.parse(data)
    c.route = c.config.resolve("+919876543210")[0]
    c.binding = {"notes": "Caller said they own this device"}
    context = c.voice_context()
    assert "configured tools" in context if admin else "restricted access" in context
    assert ("routed as admin" in context) == admin
    assert "untrusted facts" in context
    assert "Caller said they own this device" in context


async def test_remote_hangup_audio_close_race_is_not_a_session_failure():
    state = {"call": {"id": "call-1", "state": "active"}}

    class ClosingVoice(Voice):
        def healthy(self):
            state["call"]["state"] = "idle"
            return False

    controller = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                                 answer=None, end=None, make_voice=lambda *a: ClosingVoice(), api_factory=API)
    controller.call_id = "call-1"
    route = controller.config.resolve("+919876543210")[0]
    try:
        await controller.serve("call-1", "+919876543210", route)
        assert controller.status["state"] != "failed"
    finally:
        await controller.finish()


async def test_persistent_voice_failure_still_ends_the_call():
    state = {"call": {"id": "call-1", "state": "active"}}
    ended = []

    async def end(call_id):
        ended.append(call_id)

    class BrokenVoice(Voice):
        def healthy(self):
            return False

    controller = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                                 answer=None, end=end, make_voice=lambda *a: BrokenVoice(), api_factory=API)
    controller.call_id = "call-1"
    try:
        await controller.serve("call-1", "+919876543210", controller.config.resolve("+919876543210")[0])
        assert controller.status["state"] == "failed"
        assert ended == ["call-1"]
    finally:
        await controller.finish()


async def test_requested_hangup_does_not_report_voice_failure():
    state = {"call": {"id": "call-1", "state": "active", "remote_number": "+919876543210", "remote_number_verified": True}}
    ended = asyncio.Event()

    async def end(call_id):
        ended.set()
        state["call"]["state"] = "idle"

    controller = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                                 answer=None, end=end, make_voice=lambda *a: Voice(), api_factory=API)
    controller.start()
    try:
        await wait_for(lambda: controller.status["state"] == "ready")
        result = await controller.delegate(SimpleNamespace(name="end_call", arguments={}))
        assert result["status"] == "ok"
        await asyncio.wait_for(ended.wait(), 1)
        await wait_for(lambda: controller.api is None)
        assert controller.status["state"] != "failed"
    finally:
        await controller.close()


class API:
    def __init__(self, endpoint):
        self.actions = []
        self.run_id = "run-1"

    async def check(self):
        pass

    async def bind(self, *args):
        return {
            "session_id": "hfp-test",
            "caller_id": "caller",
            "notes": "Only this caller",
        }

    async def request(self, *args, **kwargs):
        return {"ok": True}

    async def revoke(self, sid):
        self.actions.append("revoke")

    async def stop(self):
        self.actions.append("stop")

    async def close(self):
        self.actions.append("close")

    async def events(self, **kwargs):
        yield {"type": "result", "status": "completed", "output": "Saved"}


class Voice:
    name = "gemini_live"

    def __init__(self):
        self.stopped = False

    async def start(self, *args):
        pass

    def healthy(self):
        return True

    async def stop(self):
        self.stopped = True


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_voice_handoff_preserves_context_and_permissions():
    captured = []

    class CapturingAPI(API):
        async def events(self, **kwargs):
            captured.append(kwargs["text"])
            yield {"type": "result", "status": "completed", "output": "Understood"}

    state = {"call": {"id": "call-1", "state": "active", "remote_number": "+919876543210", "remote_number_verified": True}}
    c = PhoneController(RoutingConfig.parse(routing_data()), snapshot=lambda: state,
                        answer=None, end=None, make_voice=lambda *args: Voice(), api_factory=CapturingAPI)
    c.start()
    try:
        await wait_for(lambda: c.status["state"] == "ready")
        policy = c.route.policy
        context = "This is fictional restaurant role-play. മലയാളത്തിൽ മറുപടി. No real order."
        result = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "Is egg optional?", "context": context}, request_id="req-context"))
        assert result["status"] == "ok"
        assert json.loads(captured[0]) == {"caller_request": "Is egg optional?", "conversation_context": context}
        assert c.route.policy == policy
        for invalid in ({"admin": True}, "x" * 8001):
            result = await c.delegate(SimpleNamespace(name="ask_hermes", arguments={"task": "Do it", "context": invalid}, request_id="bad-context"))
            assert result["status"] == "error"
        assert len(captured) == 1
    finally:
        await c.close()


async def test_routed_call_ready_then_hangup_revokes_authority():
    state = {
        "call": {
            "id": "call-1",
            "state": "incoming",
            "remote_number": "+919876543210",
            "remote_number_verified": True,
        }
    }
    ended, voices = [], []

    async def answer(call_id):
        state["call"]["state"] = "active"

    async def end(call_id):
        ended.append(call_id)
        state["call"]["state"] = "idle"

    def make_voice(mode, controller):
        voice = Voice()
        voices.append(voice)
        return voice

    controller = PhoneController(
        RoutingConfig.parse(routing_data()),
        snapshot=lambda: state,
        answer=answer,
        end=end,
        make_voice=make_voice,
        api_factory=API,
    )
    controller.start()
    try:
        await wait_for(lambda: controller.status["state"] == "ready")
        api = controller.api
        assert controller.status["profile"] == "default"
        result = await controller.delegate(
            SimpleNamespace(
                name="ask_hermes", arguments={"task": "remember this"}, request_id="req"
            )
        )
        assert result == {"status": "ok", "message": "Saved"}
        state["call"]["state"] = "idle"
        await wait_for(lambda: controller.api is None)
        assert api.actions == ["revoke", "revoke", "stop", "close"]
        assert voices[0].stopped
    finally:
        await controller.close()


async def test_unknown_number_is_declined_without_opening_hermes():
    state = {
        "call": {
            "id": "call-1",
            "state": "incoming",
            "remote_number": "+919876543211",
            "remote_number_verified": True,
        }
    }
    ended = []

    async def end(call_id):
        ended.append(call_id)
        state["call"]["state"] = "idle"

    def fail_api(_):
        raise AssertionError("unknown caller reached Hermes")

    controller = PhoneController(
        RoutingConfig.parse(routing_data()),
        snapshot=lambda: state,
        answer=None,
        end=end,
        make_voice=None,
        api_factory=fail_api,
    )
    controller.start()
    try:
        await wait_for(lambda: ended)
        assert ended == ["call-1"]
    finally:
        await controller.close()


async def test_changed_identity_ends_call_instead_of_switching_profile():
    state = {
        "call": {
            "id": "call-1",
            "state": "active",
            "remote_number": "+919876543210",
            "remote_number_verified": True,
        }
    }
    ended = []

    async def end(call_id):
        ended.append(call_id)
        state["call"]["state"] = "idle"

    controller = PhoneController(
        RoutingConfig.parse(routing_data()),
        snapshot=lambda: state,
        answer=None,
        end=end,
        make_voice=lambda *_: Voice(),
        api_factory=API,
    )
    controller.start()
    try:
        await wait_for(lambda: controller.status["state"] == "ready")
        state["call"]["remote_number"] = "+919876543211"
        await wait_for(lambda: ended)
        assert ended == ["call-1"]
    finally:
        await controller.close()
