import pytest

from hfp_mcp.routing import RoutingConfig


def routing_data():
    return {
        "version": 1,
        "endpoints": {
            "owner": {
                "profile": "default",
                "url": "http://127.0.0.1:8642",
                "token_env": "TEST_API_TOKEN",
                "bridge_token_env": "TEST_BRIDGE_TOKEN",
            }
        },
        "policies": {
            "owner": {"admin": True},
            "guest": {"tools": ["hfp_caller_read", "hfp_caller_update"]},
        },
        "numbers": {"+919876543210": {"endpoint": "owner", "policy": "owner"}},
    }


def test_optional_routes_and_explicit_guest():
    assert RoutingConfig.parse(None).resolve("+919876543210")[1] == "routing_disabled"
    data = routing_data()
    config = RoutingConfig.parse(data)
    assert config.resolve("9876543210")[1] == "number_match"
    assert config.resolve(None) == (None, "unmapped")
    assert config.resolve("+919876543210", presented=False) == (None, "unmapped")
    data["default"] = {"endpoint": "owner", "policy": "guest"}
    config = RoutingConfig.parse(data)
    assert config.resolve(None)[0].policy == "guest"
    assert config.resolve("garbage")[0].policy == "guest"


@pytest.mark.parametrize(
    "modify",
    [
        lambda d: d["numbers"].update(
            {"9876543210": {"endpoint": "owner", "policy": "guest"}}
        ),
        lambda d: d.update(blocked=["+919876543210"]),
        lambda d: d.update(default={"endpoint": "owner", "policy": "owner"}),
        lambda d: d["numbers"]["+919876543210"].update(endpoint="missing"),
        lambda d: d["endpoints"]["owner"].update(url="http://192.168.1.5:8642"),
        lambda d: d["policies"]["guest"].update(tools=["*"]),
        lambda d: d["endpoints"]["owner"].update(token_env="TEST_BRIDGE_TOKEN"),
    ],
)
def test_ambiguous_or_unsafe_routes_rejected(modify):
    data = routing_data()
    modify(data)
    with pytest.raises(ValueError):
        RoutingConfig.parse(data)


def test_full_tool_names_and_no_secrets_in_explain(monkeypatch):
    monkeypatch.setenv("TEST_API_TOKEN", "s" * 40)
    config = RoutingConfig.parse(routing_data())
    assert not config.policies["guest"].allows("malicious__hfp_caller_read")
    assert "ssss" not in str(config.explain("+919876543210"))


def test_duplicate_yaml_mapping_cannot_silently_replace_a_route(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("phone:\n  version: 1\n  version: 1\n")
    with pytest.raises(ValueError, match="duplicate configuration key"):
        RoutingConfig.load(path)
