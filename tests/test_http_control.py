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


def test_callback_http_validation_cancel_and_visible_failure(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from hfp_mcp.callbacks import CallbackScheduler
    from hfp_mcp.routing import RoutingConfig
    from tests.test_phone_routing import routing_data
    config = RuntimeConfig.load(environ={'HFP_MCP_CONFIG': str(tmp_path / 'missing.yaml'),
                                        'HFP_MCP_DATABASE': str(tmp_path / 'calls.db')})
    scheduler = CallbackScheduler(config.database_file, None)
    monkeypatch.setattr(server, '_callback_scheduler', scheduler)
    monkeypatch.setattr(server, '_audio_stream_server', None)
    monkeypatch.setattr(server.mcp, '_session_manager', None)
    app = server.create_http_app(config, 'x' * 48, start_bluetooth=False)
    headers = {'Authorization': 'Bearer ' + 'x' * 48}
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.post('/v1/phone/callbacks', json={}).status_code == 401
        assert client.post('/v1/phone/callbacks/cb-req/cancel', json={}).status_code == 401
        controller = SimpleNamespace(config=RoutingConfig.parse({**routing_data(), 'blocked': ['+919876543211']}), status={'state': 'idle'})
        body = dict(request_id='req', number='+919876543210', purpose='Appointment', run_at=time.time() + 60)
        # Patch controller only around requests so app cleanup does not close a stub.
        with monkeypatch.context() as scoped:
            scoped.setattr(server, '_phone_controller', controller)
            assert client.post('/v1/phone/callbacks', json={**body, 'number': '+919876543211'}, headers=headers).status_code == 403
            created = client.post('/v1/phone/callbacks', json=body, headers=headers).json()
            assert created['ok'] and created['warning']
            assert client.post('/v1/phone/callbacks/cb-req/reminder', json={'job_id': 'job-1'}, headers=headers).json()['ok']
            cancelled = client.post('/v1/phone/callbacks/cb-req/cancel', json={}, headers=headers).json()
            assert cancelled['callback']['status'] == 'cancelled'
            assert cancelled['callback']['reminder_job_id'] == 'job-1'
            scheduler._finish('cb-req', status='failed', error='Phone not connected')
            from hfp_mcp.routing import RoutingConfig as RC
            scoped.setattr(RC, 'load', classmethod(lambda cls: RC()))
            status = client.get('/v1/phone', headers=headers).json()
            assert not status['scheduled_callbacks']
            assert status['recent_callbacks'][0]['error'] == 'Phone not connected'
