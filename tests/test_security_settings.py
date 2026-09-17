import os
import stat

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.endpoints import WebSocketEndpoint
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient, WebSocketDisconnect

from hfp_mcp.security import (
    BearerAuthMiddleware,
    HostOriginMiddleware,
    load_or_create_token,
)
from hfp_mcp.settings import RuntimeConfig, _load_service_environment


def test_token_file_is_created_private_and_reused(tmp_path):
    path = tmp_path / "control.token"
    token = load_or_create_token(path)
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == token


def test_token_file_with_broad_permissions_is_rejected(tmp_path):
    path = tmp_path / "control.token"
    path.write_text("x" * 40)
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_or_create_token(path)


def test_bearer_middleware_protects_control_but_not_health():
    async def endpoint(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", endpoint), Route("/healthz", endpoint)])
    app.add_middleware(BearerAuthMiddleware, token="t" * 40)
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": f"Bearer {'t' * 40}"}).status_code == 200


def test_runtime_config_refuses_plaintext_lan(tmp_path):
    base = {
        "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
        "HFP_MCP_HOST": "0.0.0.0",
        "HFP_MCP_TOKEN_FILE": str(tmp_path / "token"),
    }
    with pytest.raises(ValueError, match="direct TLS"):
        RuntimeConfig.load(environ=base)

    with pytest.raises(ValueError, match="loopback backend"):
        RuntimeConfig.load(
            environ={
                **base,
                "HFP_MCP_TRUSTED_TLS_PROXY": "true",
                "HFP_MCP_PUBLIC_BASE_URL": "https://phone.example.lan",
            }
        )

    secure = RuntimeConfig.load(
        environ={
            **base,
            "HFP_MCP_HOST": "127.0.0.1",
            "HFP_MCP_TRUSTED_TLS_PROXY": "true",
            "HFP_MCP_PUBLIC_BASE_URL": "https://phone.example.lan",
        }
    )
    assert secure.host == "127.0.0.1"
    assert secure.trusted_tls_proxy is True


def test_daemon_url_is_loaded_for_stdio_proxy_and_must_be_secure(tmp_path):
    secure = RuntimeConfig.load(
        environ={
            "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
            "HFP_PHONE_MCP_URL": "https://phone.example.lan/mcp",
        }
    )
    assert secure.daemon_url == "https://phone.example.lan/mcp"

    with pytest.raises(ValueError, match="non-loopback daemon_url"):
        RuntimeConfig.load(
            environ={
                "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
                "HFP_PHONE_MCP_URL": "http://phone.example.lan/mcp",
            }
        )


def test_runtime_config_reflects_hermes_admin_bypass_for_doctor(tmp_path):
    config = RuntimeConfig.load(
        environ={
            "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
            "HFP_PHONE_ADMIN_APPROVAL_BYPASS": "true",
        }
    )

    assert config.admin_approval_mode == "bypass"


def test_runtime_config_loads_and_validates_handset_self_number(tmp_path):
    config = RuntimeConfig.load(
        environ={
            "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
            "HFP_DEFAULT_REGION": "IN",
            "HFP_PHONE_SELF_NUMBER": "9074972348",
        }
    )

    assert config.self_number == "9074972348"

    with pytest.raises(ValueError, match="phone number"):
        RuntimeConfig.load(
            environ={
                "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
                "HFP_PHONE_SELF_NUMBER": "not-a-number",
            }
        )


def test_runtime_config_loads_roles_and_dedicated_atd_timeout(tmp_path):
    config = RuntimeConfig.load(
        environ={
            "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
            "HFP_DEFAULT_REGION": "IN",
            "HFP_PHONE_OWNER_NUMBER": "7907686219",
            "HFP_PHONE_ADMIN_CALLERS": "+917907686219,+919999999999",
            "HFP_PHONE_TRUSTED_CALLERS": "+918888888888",
            "HFP_PHONE_BLOCKED_CALLERS": "+917777777777",
            "HFP_AT_DIAL_TIMEOUT_SECONDS": "15",
        }
    )

    assert config.owner_number == "7907686219"
    assert config.admin_callers == ("+917907686219", "+919999999999")
    assert config.trusted_callers == ("+918888888888",)
    assert config.blocked_callers == ("+917777777777",)
    assert config.at_dial_timeout_seconds == 15.0

    with pytest.raises(ValueError, match="AT dial timeout"):
        RuntimeConfig.load(
            environ={
                "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
                "HFP_AT_DIAL_TIMEOUT_SECONDS": "0.5",
            }
        )


def test_runtime_config_brackets_ipv6_host_and_origin_defaults():
    config = RuntimeConfig(host="::1", public_host="::1")

    assert "[::1]:*" in config.resolved_allowed_hosts()
    assert "http://[::1]:*" in config.resolved_allowed_origins()


def test_private_service_environment_is_parsed_without_shell_expansion(tmp_path):
    path = tmp_path / "hfp-mcp.env"
    path.write_text(
        'HFP_MCP_HOST="127.0.0.1"\n'
        'HFP_MCP_OPTS="--port 8000 --not-executed $(touch /tmp/nope)"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    values = _load_service_environment(path)

    assert values["HFP_MCP_HOST"] == "127.0.0.1"
    assert "$(touch /tmp/nope)" in values["HFP_MCP_OPTS"]


def test_broad_service_environment_permissions_are_rejected(tmp_path):
    path = tmp_path / "hfp-mcp.env"
    path.write_text("HFP_MCP_HOST=127.0.0.1\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(PermissionError, match="mode 0600"):
        _load_service_environment(path)


def test_host_origin_policy_covers_websocket_routes():
    class Echo(WebSocketEndpoint):
        encoding = "text"

        async def on_connect(self, websocket):
            await websocket.accept()

    app = Starlette(routes=[WebSocketRoute("/audio", Echo)])
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=("testserver",),
        allowed_origins=("https://trusted.example",),
    )
    client = TestClient(app)

    with client.websocket_connect("/audio"):
        pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/audio", headers={"host": "evil.example"}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/audio", headers={"origin": "https://evil.example"}
        ):
            pass


def test_host_origin_policy_treats_bracketed_ipv6_as_literal():
    async def endpoint(_request):
        return JSONResponse({"ok": True})

    class Echo(WebSocketEndpoint):
        encoding = "text"

        async def on_connect(self, websocket):
            await websocket.accept()

    app = Starlette(
        routes=[Route("/health", endpoint), WebSocketRoute("/audio", Echo)]
    )
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=("[::1]", "[::1]:*"),
        allowed_origins=("http://[::1]:*",),
    )
    client = TestClient(app)

    assert client.get("/health", headers={"host": "[::1]:8000"}).status_code == 200
    with client.websocket_connect(
        "/audio",
        headers={"host": "[::1]:8000", "origin": "http://[::1]:8000"},
    ):
        pass
