import asyncio
import sys
from types import ModuleType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


def bind(store, sid, caller="+919876543210"):
    return store.bind(session_id=sid, call_id="call", profile="default", number=caller,
                      policy={"admin": True, "tools": []}, ttl=10)


def test_native_session_reuse_cannot_revive_a_revoked_run(tmp_path, monkeypatch):
    store = CallerStore(tmp_path / "callers.sqlite3")
    bind(store, "hfp-old")
    root = store.manage("a"*32, "hfp-old")
    store.bind_run("run-old", "hfp-old", root)
    module = ModuleType("tools.approval_context")
    current = ["run-old"]
    module.get_current_session_key = lambda: current[0]
    monkeypatch.setitem(sys.modules, "tools.approval_context", module)
    bridge = PhoneBridge(RoutingConfig.parse(routing_data()), store, profile="default", profile_config={})
    assert bridge.before_tool(session_id=root, tool_name="terminal") is None
    store.revoke("hfp-old")
    bind(store, "hfp-new")
    store.bind_run("run-new", "hfp-new", root)
    assert bridge.before_tool(session_id=root, tool_name="terminal")["action"] == "block"
    # A native compression child may have been deleted or orphaned. The old
    # execution remains a phone run even without a usable session parent chain.
    assert bridge.before_tool(session_id="deleted-native-compression-child", tool_name="terminal")["action"] == "block"
    current[0] = "run-new"
    assert bridge.before_tool(session_id=root, tool_name="terminal") is None
    with store.db:
        store.db.execute("UPDATE bindings SET expires=0 WHERE session_id='hfp-new'")
    assert bridge.before_tool(session_id=root, tool_name="terminal")["action"] == "block"
    with pytest.raises(PermissionError):
        store.bind_run("run-old", "hfp-new", root)
    bind(store, "hfp-bob", "+919876543211")
    with pytest.raises(PermissionError):
        store.check_managed(root, "hfp-bob")
    store.retire_managed(root)
    assert bridge.before_tool(session_id=root, tool_name="terminal")["action"] == "block"
    store.close()


async def test_native_admission_binds_before_execution_and_replay_cannot_rebind(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b"*40)
    store = CallerStore(tmp_path / "callers.sqlite3")
    bind(store, "hfp-first")
    root = store.manage("a"*32, "hfp-first")
    bridge = PhoneBridge(RoutingConfig.parse(routing_data()), store, profile="default", profile_config={})
    observed, tasks = [], []
    async def execute():
        observed.append(store.run_binding("run-test")["binding_id"])
    async def submit(request):
        if not tasks:
            tasks.append(asyncio.create_task(execute()))
        return web.json_response({"run_id": "run-test"}, status=202)
    app = web.Application()
    app.router.add_post("/v1/runs", submit)
    bridge.wire(app)
    headers = {"X-HFP-Bridge-Token": "b"*40, "X-HFP-Binding": "hfp-first"}
    async with TestClient(TestServer(app)) as client:
        bad = await client.post("/v1/runs", json={"session_id": root})
        assert bad.status == 401
        response = await client.post("/v1/runs", json={"session_id": root}, headers=headers)
        assert response.status == 202
        await asyncio.gather(*tasks)
        assert observed == ["hfp-first"]
        bind(store, "hfp-later")
        response = await client.post("/v1/runs", json={"session_id": root}, headers={**headers,"X-HFP-Binding":"hfp-later"})
        assert response.status == 403
        assert store.run_binding("run-test")["binding_id"] == "hfp-first"
    store.close()
