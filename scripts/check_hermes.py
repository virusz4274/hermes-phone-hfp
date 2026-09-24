"""Exercise real Hermes plugin/session APIs in a disposable, credential-free home."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


async def exercise():
    # All provider/network attempts must fail; this test uses only in-process HTTP.
    def no_network(*args, **kwargs):
        raise AssertionError(
            "Hermes compatibility checks must not contact external services"
        )

    socket.socket.connect = no_network
    socket.create_connection = no_network

    import yaml
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from hermes_cli.plugins import (
        PluginContext,
        PluginManager,
        PluginManifest,
        LoadedPlugin,
    )
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hfp_mcp import hermes_bridge, hermes_sessions
    from hfp_mcp.hermes_compat import native_readiness, memory_readiness, REQUIRED_RUN_FEATURES

    home = Path(os.environ["HERMES_HOME"])
    config = {
        "model": {"default": "test/model", "provider": "openrouter"},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "plugins": {"enabled": ["hfp-phone"]},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    route = {
        "phone": {
            "version": 1,
            "default_region": "IN",
            "endpoints": {
                "personal": {
                    "profile": "default",
                    "url": "http://127.0.0.1:8642",
                    "token_env": "TEST_API",
                    "bridge_token_env": "TEST_BRIDGE",
                }
            },
            "policies": {"admin": {"admin": True}},
            "numbers": {
                "+919876543210": {
                    "endpoint": "personal",
                    "policy": "admin",
                    "voice": "classic",
                }
            },
        }
    }
    routing = home / "phone.yaml"
    routing.write_text(yaml.safe_dump(route))
    os.environ.update(
        HFP_MCP_CONFIG=str(routing), TEST_API="a" * 40, TEST_BRIDGE="b" * 40
    )
    manager = PluginManager(scope_key=str(home))
    manifest = PluginManifest(name="hfp-phone", version="0.1.0rc1", source="user")
    manager._plugins["hfp-phone"] = LoadedPlugin(manifest=manifest, enabled=True)
    context = PluginContext(manifest, manager)
    hermes_bridge.register(context)
    assert manager._hooks.get("pre_tool_call") and manager._hooks.get("pre_llm_call")
    assert manager._platform_handler_factories.get("api_server")
    bridge = hermes_bridge._BRIDGES[-1]
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "a" * 40}))
    assert native_readiness(adapter)["sessions"]
    assert native_readiness(adapter)["compaction"]
    assert memory_readiness(adapter)
    request = make_mocked_request(
        "GET", "/v1/capabilities", headers={"Authorization": "Bearer " + "a" * 40}
    )
    response = await adapter._handle_capabilities(request)
    capabilities = json.loads(response.text)
    assert REQUIRED_RUN_FEATURES <= {
        k for k, v in capabilities["features"].items() if v
    }

    app = web.Application()
    factory = manager._platform_handler_factories["api_server"][0][0]
    factory(app, adapter)
    # Exercise the registered endpoint with the real native adapter and auth gate.
    handler = next(
        r.handler
        for r in app.router.routes()
        if r.method == "GET" and r.resource.canonical == "/v1/hfp/capabilities"
    )
    response = await handler(
        make_mocked_request(
            "GET", "/v1/hfp/capabilities", headers={"X-HFP-Bridge-Token": "b" * 40}
        )
    )
    assert json.loads(response.text)["conversation_sessions"]
    try:
        await handler(make_mocked_request("GET", "/v1/hfp/capabilities"))
    except web.HTTPUnauthorized:
        pass
    else:
        raise AssertionError("Bridge capability endpoint accepted missing credentials")

    store = bridge.store
    store.bind(
        session_id="hfp-test",
        call_id="test-call",
        profile="default",
        number="+919876543210",
        policy={"admin": True, "tools": []},
        ttl=300,
    )
    root = store.manage("a" * 32, "hfp-test")
    assert hermes_sessions.create(store, root, "default") == root
    assert hermes_sessions.create(store, root, "default") == root
    store.bind_run("run-test", "hfp-test", root)
    from tools.approval_context import set_current_session_key

    # Context propagation is the actual Hermes run identity API.
    set_current_session_key("run-test")
    assert bridge.before_tool(session_id=root, tool_name="terminal") is None
    store.revoke("hfp-test")
    assert (
        bridge.before_tool(session_id=root, tool_name="terminal")["action"] == "block"
    )
    set_current_session_key(None)

    with hermes_sessions.native_db(store) as db:
        db.append_message(root, "user", "Synthetic phone dialogue")
        db.append_message(root, "assistant", "Synthetic answer")
    # Exercise the native no-op compaction path without a provider request.
    result = hermes_sessions.compact(store, root, adapter, "default")
    assert result["status"] == "ok" and "too little" in result["message"]
    child = root + "-compressed"
    with hermes_sessions.native_db(store) as db:
        # Native atomic publication validates the continuation lineage. The summary
        # is synthetic; model summarization quality is a live acceptance check.
        db.publish_compression_child(
            parent_session_id=root,
            child_session_id=child,
            source="api_server",
            messages=[
                {"role": "user", "content": "Synthetic retained context"},
                {"role": "assistant", "content": "Ready"},
            ],
            profile_name="default",
            require_compression_lease=False,
        )
        assert hermes_sessions.lineage(db, root) == [child, root]
    assert hermes_sessions.create(store, root, "default") == child
    assert hermes_sessions.managed_root(store, child) == root
    hermes_sessions.remove(store, root)
    with hermes_sessions.native_db(store, read_only=True) as db:
        assert db.get_session(root) is None and db.get_session(child) is None
    if hasattr(bridge, "tasks"):
        bridge.tasks.capacity.db.close()
    store.close()
    print(
        "PASS: native plugin registration, capability/auth gates, sessions, caller revocation, compaction lineage and deletion"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        asyncio.run(exercise())
        return
    if not args.hermes_root or not (args.hermes_root / "hermes_state.py").is_file():
        parser.error("--hermes-root must identify a Hermes source checkout")
    with tempfile.TemporaryDirectory(prefix="hfp-hermes-compat-") as directory:
        home = Path(directory)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": directory,
            "HERMES_HOME": directory,
            "HERMES_PROFILE": "default",
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "HFP_MCP_ENV_FILE": str(home / "missing.env"),
            "HF_HUB_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[1] / "src"),
                    str(args.hermes_root.resolve()),
                ]
            ),
        }
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            cwd=directory,
            env=env,
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    main()
