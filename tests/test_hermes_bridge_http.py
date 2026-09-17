import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


async def test_binding_routes_require_bridge_secret_and_derive_policy_from_number(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b" * 40)
    config = RoutingConfig.parse(routing_data())
    store = CallerStore(tmp_path / "callers.sqlite3")
    bridge = PhoneBridge(config, store, profile="default", profile_config={})
    app = web.Application()
    bridge.wire(app)
    async with TestClient(TestServer(app)) as client:
        body = {
            "number": "+919876543210",
            "call_id": "call-a",
            "endpoint": "owner",
            "policy": "owner",
        }
        response = await client.post("/v1/hfp/bindings", json=body)
        assert response.status == 401
        headers = {"X-HFP-Bridge-Token": "b" * 40}
        response = await client.post(
            "/v1/hfp/bindings",
            json={**body, "number": "+919876543211"},
            headers=headers,
        )
        assert response.status == 400
        response = await client.post("/v1/hfp/bindings", json=body, headers=headers)
        assert response.status == 200
        binding = await response.json()
        sid = binding["session_id"]
        assert store.binding(sid)["policy"]["admin"] is True
        response = await client.post(f"/v1/hfp/bindings/{sid}/renew", headers=headers)
        assert response.status == 200
        response = await client.delete(f"/v1/hfp/bindings/{sid}", headers=headers)
        assert response.status == 200
        response = await client.post(f"/v1/hfp/bindings/{sid}/renew", headers=headers)
        assert response.status == 409
        with pytest.raises(PermissionError):
            bridge.authorize(sid, "terminal")
    store.close()


async def test_explicit_profile_prefix_never_binds_other_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b" * 40)
    config = RoutingConfig.parse(routing_data())
    store = CallerStore(tmp_path / "callers.sqlite3")
    bridge = PhoneBridge(config, store, profile="default", profile_config={})
    app = web.Application()
    bridge.wire(app, prefix="/p/default")
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/p/default/v1/hfp/capabilities", headers={"X-HFP-Bridge-Token": "b" * 40}
        )
        assert response.status == 200
        assert (await response.json())["profile"] == "default"
        response = await client.get(
            "/p/wrong/v1/hfp/capabilities", headers={"X-HFP-Bridge-Token": "b" * 40}
        )
        assert response.status == 404
    store.close()
