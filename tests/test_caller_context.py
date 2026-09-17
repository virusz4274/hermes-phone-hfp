import pytest

from hfp_mcp.caller_context import CallerStore
from hfp_mcp.hermes_bridge import PhoneBridge
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.fixture
def store(tmp_path):
    store = CallerStore(tmp_path / "callers.sqlite3")
    yield store
    store.close()


def bind(store, sid="hfp-a", profile="guest", number="+919876543210", ttl=10, **kwargs):
    return store.bind(
        session_id=sid,
        call_id="call-a",
        profile=profile,
        number=number,
        policy={"admin": False, "tools": ["hfp_caller_read", "hfp_caller_update"]},
        ttl=ttl,
        **kwargs,
    )


def test_caller_notes_survive_new_call_but_do_not_cross_callers_or_profiles(store):
    first = bind(store)
    store.update_for_session("hfp-a", "Prefers afternoon appointments")
    again = bind(store, "hfp-next")
    other = bind(store, "hfp-other", number="+919876543211")
    assert first["caller_id"] == again["caller_id"]
    assert store.read("guest", again["caller_id"]) == "Prefers afternoon appointments"
    assert store.read("guest", other["caller_id"]) == ""
    assert store.read("owner", first["caller_id"]) == ""
    store.forget("guest", first["caller_id"])
    assert store.read("guest", first["caller_id"]) == ""


def test_revoked_or_expired_call_cannot_update_or_renew(store):
    bind(store)
    store.revoke("hfp-a")
    with pytest.raises(PermissionError):
        store.update_for_session("hfp-a", "late result")
    with pytest.raises(PermissionError):
        store.renew("hfp-a")
    with pytest.raises(PermissionError):
        bind(store, "hfp-expired", ttl=-1)
    with pytest.raises(PermissionError):
        store.binding("hfp-expired")
    with pytest.raises(ValueError):
        bind(store)


def test_withheld_callers_never_share_persistent_identity(store):
    first = bind(store, number=None)
    second = bind(store, "hfp-b", number=None)
    assert first["caller_id"] != second["caller_id"]
    with pytest.raises(PermissionError):
        store.update_for_session("hfp-a", "private")


def test_phone_hooks_and_execution_guard_fail_closed(store):
    bind(store)
    bridge = PhoneBridge(
        RoutingConfig.parse(routing_data()), store, profile="guest", profile_config={}
    )
    assert bridge.before_tool(session_id="hfp-a", tool_name="hfp_caller_read") is None
    assert (
        bridge.before_tool(session_id="hfp-a", tool_name="terminal")["action"]
        == "block"
    )
    assert (
        bridge.before_tool(session_id="hfp-missing", tool_name="time")["action"]
        == "block"
    )
    assert bridge.before_tool(session_id="normal-chat", tool_name="terminal") is None
    with pytest.raises(PermissionError):
        bridge.authorize("hfp-a", "mcp__evil__hfp_caller_read")
    wrong = PhoneBridge(bridge.config, store, profile="owner", profile_config={})
    with pytest.raises(PermissionError):
        wrong.authorize("hfp-a", "hfp_caller_read")
    store.revoke("hfp-a")
    assert (
        bridge.before_tool(session_id="hfp-a", tool_name="hfp_caller_read")["action"]
        == "block"
    )


def test_reception_profile_cannot_load_shared_private_memory(store):
    config = RoutingConfig.parse(routing_data())
    bridge = PhoneBridge(config, store, profile="guest", profile_config={})
    with pytest.raises(ValueError, match="shared memory"):
        bridge.restricted_ready(config.policies["guest"])
    bridge.profile_config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "platform_toolsets": {"api_server": ["hfp_caller"]},
    }
    bridge.restricted_ready(config.policies["guest"])
    bridge.profile_config["mcp_servers"] = {"calendar": {}}
    with pytest.raises(ValueError, match="raw MCP"):
        bridge.restricted_ready(config.policies["guest"])


def test_cancelled_request_cannot_borrow_next_requests_authority(store):
    first = bind(store, "hfp-first")
    store.revoke("hfp-first")
    second = bind(store, "hfp-second")
    assert first["caller_id"] == second["caller_id"]
    with pytest.raises(PermissionError):
        store.update_for_session("hfp-first", "late cancelled write")
    store.update_for_session("hfp-second", "confirmed new request")
    assert store.read("guest", second["caller_id"]) == "confirmed new request"
