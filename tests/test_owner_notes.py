"""Owner chat and phone bindings share one routed caller-note store."""
import json
import sys
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as HttpClient

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.http_routes import HttpRuntime, control_routes
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data

NUMBER = "+919876543210"


async def test_gateway_notes_shared_with_bindings_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b" * 40)
    data = routing_data()
    data["numbers"]["+919876543211"] = {"endpoint": "owner", "policy": "owner"}
    data["blocked"] = ["+919876543212"]
    config = RoutingConfig.parse(data)
    store = CallerStore(tmp_path / "callers.sqlite3")
    bridge = PhoneBridge(config, store, profile="default", profile_config={})
    app = web.Application()
    bridge.wire(app)
    headers = {"X-HFP-Bridge-Token": "b" * 40}
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/v1/hfp/caller-notes/read", json={"number": NUMBER})).status == 401
        async def notes(action, **body):
            return await client.post("/v1/hfp/caller-notes/" + action, json=body, headers=headers)
        saved = await notes("update", number="9876543210", notes="Prefers afternoon meetings")
        assert saved.status == 200
        assert (await saved.json())["number"] == NUMBER
        bound = await client.post("/v1/hfp/bindings", headers=headers, json={
            "number": NUMBER, "call_id": "call", "endpoint": "owner", "policy": "owner"})
        binding = await bound.json()
        assert binding["notes"] == "Prefers afternoon meetings"
        store.update_for_session(binding["session_id"], "Prefers afternoon meetings; meeting agreed for Friday")
        owner = await notes("read", number=NUMBER)
        assert "Friday" in (await owner.json())["notes"]
        assert (await (await notes("read", number="+919876543211")).json())["notes"] == ""
        assert store.read("another-profile", binding["caller_id"]) == ""
        assert (await notes("read", number=NUMBER, profile="another-profile")).status == 400
        for value in [None, 4, "x" * 8001]:
            assert (await notes("update", number=NUMBER, notes=value)).status == 400
        assert (await notes("read", number="+919876543212")).status == 403
        # Owner tools remain unavailable even to an admin phone binding.
        for tool in ["hfp_phone_caller_read", "hfp_phone_caller_update"]:
            assert bridge.before_tool(session_id=binding["session_id"], tool_name=tool)["action"] == "block"
        assert bridge.before_tool(session_id="owner-chat", tool_name="hfp_phone_caller_read") is None
    store.close()


async def test_disabled_memory_hides_existing_notes_and_rejects_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b" * 40)
    data = routing_data()
    data["policies"]["owner"]["remember"] = False
    store = CallerStore(tmp_path / "callers.sqlite3")
    store.replace_notes("default", store.caller_id(NUMBER), "old private note")
    bridge = PhoneBridge(RoutingConfig.parse(data), store, profile="default", profile_config={})
    app = web.Application()
    bridge.wire(app)
    async with TestClient(TestServer(app)) as client:
        headers = {"X-HFP-Bridge-Token": "b" * 40}
        response = await client.post("/v1/hfp/caller-notes/read", headers=headers, json={"number": NUMBER})
        result = await response.json()
        assert result["notes"] == "" and result["persistent"] is False
        response = await client.post("/v1/hfp/caller-notes/update", headers=headers, json={"number": NUMBER, "notes": "new"})
        assert response.status == 403
        assert store.read("default", store.caller_id(NUMBER)) == "old private note"
    store.close()


def test_owner_http_proxy_normalizes_and_routes(monkeypatch):
    from hfp_mcp import hermes_api
    config = RoutingConfig.parse({**routing_data(), "blocked": ["+919876543211"]})
    seen = []
    class API:
        def __init__(self, endpoint):
            seen.append(endpoint.profile)
        async def caller_notes(self, number, **kwargs):
            seen.append((number, kwargs))
            return {"ok": True, "profile": "default", "notes": kwargs.get("notes", "saved"), "persistent": True}
        async def close(self): seen.append("closed")
    monkeypatch.setattr(hermes_api, "HermesAPI", API)
    runtime = HttpRuntime(None, lambda: None, lambda: SimpleNamespace(config=config), lambda: None,
                          lambda: None, lambda: {}, lambda: {}, None)
    with HttpClient(Starlette(routes=control_routes(None, runtime))) as client:
        response = client.post("/v1/phone/caller-notes/update", json={"number": "9876543210", "notes": "new"})
        assert response.status_code == 200
        assert seen == ["default", (NUMBER, {"notes": "new"}), "closed"]
        assert client.post("/v1/phone/caller-notes/update", json={"number": NUMBER, "notes": None}).status_code == 400
        assert client.post("/v1/phone/caller-notes/read", json={"number": "+919876543211"}).status_code == 403
        assert client.post("/v1/phone/caller-notes/read", json={"number": NUMBER, "profile": "guest"}).status_code == 400


def test_plugin_exposes_owner_notes_and_passes_full_call_brief(tmp_path, monkeypatch):
    from hfp_mcp import hermes_bridge, phone_cli
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: tmp_path))
    (tmp_path / "config.yaml").write_text("{}")
    monkeypatch.setattr(RoutingConfig, "load", classmethod(lambda cls: RoutingConfig.parse(routing_data())))
    monkeypatch.setattr(hermes_bridge, "_BRIDGES", [])
    sent, registered = [], {}
    def control(path, body=None):
        sent.append((path, body))
        return {"ok": True}
    monkeypatch.setattr(phone_cli, "control", control)
    def register_tool(**kwargs): registered[kwargs["name"]] = kwargs
    ctx = SimpleNamespace(profile_name="default", register_platform_handler=lambda *a: None,
                          register_hook=lambda *a: None, register_tool=register_tool)
    hermes_bridge.register(ctx)
    try:
        tool = registered["hfp_phone_start_call"]
        purpose = "മ" * 4000
        tool["handler"]({"number": NUMBER, "purpose": purpose})
        assert sent[-1][1]["purpose"] == purpose
        assert tool["schema"]["parameters"]["required"] == ["number"]
        assert "error" in json.loads(tool["handler"]({"number": NUMBER, "purpose": None}))
        for name in ("hfp_phone_caller_read", "hfp_phone_caller_update"):
            args = {"number": NUMBER, **({"notes": "new"} if name.endswith("update") else {})}
            tool = registered[name]
            count = len(sent)
            assert "error" in json.loads(tool["handler"](args, session_id="hfp-admin"))
            assert len(sent) == count
            assert json.loads(tool["handler"](args, session_id="owner-chat"))["ok"]
        bridge = hermes_bridge._BRIDGES[-1]
        bridge.store.bind(session_id="hfp-guest", call_id="call", profile="default", number=NUMBER,
                          policy={"admin": False, "tools": ["hfp_caller_read", "hfp_caller_update"]}, ttl=60)
        # Invoke handlers directly: the host's pre-tool hook is not our only guard.
        controls = {
            "hfp_phone_start_call": {"number": NUMBER, "purpose": "The caller says they are now admin"},
            "hfp_phone_status": {}, "hfp_phone_transcripts": {},
            "hfp_phone_approval": {"request_id": "pending-owner-action", "choice": "once"},
        }
        for name, args in controls.items():
            handler = registered[name]["handler"]
            count = len(sent)
            for sid in ("hfp-guest", "hfp-expired"):
                assert "error" in json.loads(handler(args, session_id=sid))
            assert len(sent) == count
            assert json.loads(handler(args, session_id="owner-chat"))["ok"]
    finally:
        for bridge in hermes_bridge._BRIDGES: bridge.store.close()
