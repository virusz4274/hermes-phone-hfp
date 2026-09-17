from starlette.testclient import TestClient

from hfp_mcp import server
from hfp_mcp.settings import RuntimeConfig


def test_unified_http_control_requires_bearer_and_exposes_versioned_state(tmp_path, monkeypatch):
    config = RuntimeConfig.load(
        environ={
            "HFP_MCP_CONFIG": str(tmp_path / "missing.yaml"),
            "HFP_MCP_HOST": "127.0.0.1",
            "HFP_MCP_TOKEN_FILE": str(tmp_path / "token"),
            "HFP_MCP_DATABASE": str(tmp_path / "calls.db"),
        }
    )
    token = "c" * 48
    monkeypatch.setattr(server, "_audio_stream_server", None)
    app = server.create_http_app(config, token, start_bluetooth=False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["ready"] is False
        readiness = client.get("/readyz")
        assert readiness.status_code == 503
        assert readiness.json()["ok"] is False
        assert readiness.json()["ready"] is False
        assert client.get("/v1/state").status_code == 401
        state = client.get(
            "/v1/state",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert state.status_code == 200
        assert state.json()["schema_version"] == "hfp.v1"
        assert isinstance(state.json()["connection"], dict)
        assert client.get(
            "/status",
            headers={"Authorization": f"Bearer {token}"},
        ).headers["deprecation"] == "true"
