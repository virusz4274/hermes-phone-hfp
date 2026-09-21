from types import SimpleNamespace

from hfp_mcp import server
from hfp_mcp.routing import RoutingConfig
from hfp_mcp.state import CallState
from tests.test_phone_routing import routing_data


async def test_controller_reconnect_supplies_idempotency_identity(monkeypatch):
    calls = []

    async def connect(address, request_id):
        calls.append((address, request_id))
        return {"ok": True}

    monkeypatch.setattr(server, "ensure_phone_connected", connect)
    monkeypatch.setattr(
        server, "_runtime_config", SimpleNamespace(device_address="AA:BB:CC:DD:EE:FF")
    )
    controller = server._create_phone_controller(RoutingConfig.parse(routing_data()))
    await controller.connect()
    assert calls[0][0] == "AA:BB:CC:DD:EE:FF"
    assert calls[0][1].startswith("phone-connect-")


async def test_interactive_start_honors_blocked_numbers_and_reports_voice_ready(monkeypatch):
    state = SimpleNamespace(call_id="call-a", versioned_snapshot=lambda: {})
    controller = SimpleNamespace(
        config=RoutingConfig.parse({**routing_data(), "blocked": ["+919876543211"]}),
        discard_outbound=lambda _: None,
        status={
            "call_id": "call-a",
            "state": "ready",
            "voice": "gemini_live",
            "profile": "default",
        },
    )
    calls = []

    async def place(number, request_id, **kwargs):
        calls.append(number)
        return {"ok": True, "result": {"call_id": "call-a"}}

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_phone_controller", controller)
    monkeypatch.setattr(server, "_place_call", place)
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "_runtime_config", None)
    denied = await server.start_phone_call("+919876543211", "request-a")
    assert denied["ok"] is False
    assert calls == []
    ready = await server.start_phone_call("+919876543210", "request-b")
    assert ready["ok"] is True
    assert ready["result"]["voice"] == "gemini_live"


async def test_one_shot_excludes_call_from_later_automatic_voice(monkeypatch):
    state = SimpleNamespace(call_id=None, call_state=CallState.IDLE, audio_active=False)
    controller = SimpleNamespace(call_id=None, suspended=0, excluded_call_id=None)

    async def dial(*args):
        assert controller.suspended == 1
        state.call_id = "announcement"
        state.call_state = CallState.ACTIVE
        return {"ok": True}

    async def play(*args):
        assert controller.excluded_call_id == "announcement"
        return {"ok": True}

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_phone_controller", controller)
    monkeypatch.setattr(server, "dial_and_wait", dial)
    monkeypatch.setattr(server, "play_audio_file", play)
    result = await server.dial_and_play_audio_file("+919876543210", "example.wav")
    assert result["ok"]
    assert controller.suspended == 0
    assert controller.excluded_call_id == "announcement"
