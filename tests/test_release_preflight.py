import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hfp_mcp import diagnostics, preflight
from hfp_mcp.hermes_api import HermesAPI, REQUIRED
from hfp_mcp.routing import Endpoint, Route, RoutingConfig
from hfp_mcp.settings import RuntimeConfig


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("TEST_RELEASE_API", "a" * 40)
    monkeypatch.setenv("TEST_RELEASE_BRIDGE", "b" * 40)
    return Endpoint(
        "default", "http://localhost:8642", "TEST_RELEASE_API", "TEST_RELEASE_BRIDGE"
    )


@pytest.mark.parametrize(
    "case", ["ok", "auth", "profile", "capabilities", "timeout", "continuity"]
)
async def test_endpoint_preflight_uses_only_get_and_redacts_errors(
    monkeypatch, endpoint, case
):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if case == "timeout":
            raise httpx.ReadTimeout("PRIVATE CREDENTIAL", request=request)
        if case == "auth":
            return httpx.Response(401, text="PRIVATE CREDENTIAL")
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200, json={"features": {k: case != "capabilities" for k in REQUIRED}}
            )
        assert request.headers["X-HFP-Bridge-Token"] == "b" * 40
        return httpx.Response(
            200,
            json={
                "version": 1,
                "profile": "wrong" if case == "profile" else "default",
                "conversation_sessions": False,
                "parallel_phone_tasks": False,
            },
        )

    monkeypatch.setattr(
        preflight,
        "HermesAPI",
        lambda e: HermesAPI(e, transport=httpx.MockTransport(handle)),
    )
    results = await preflight.endpoint_checks(
        "personal",
        endpoint,
        [Route("personal", "admin", continuity=case == "continuity")],
    )
    assert bool([c for c in results if c.status == "error"]) == (case != "ok")
    assert requests
    assert "PRIVATE CREDENTIAL" not in repr(results)
    assert "a" * 40 not in repr(results)


async def test_missing_secret_never_contacts_gateway(monkeypatch, endpoint):
    monkeypatch.delenv("TEST_RELEASE_API")
    monkeypatch.setattr(
        preflight, "HermesAPI", lambda _: pytest.fail("must not contact gateway")
    )
    assert (await preflight.endpoint_checks("personal", endpoint, []))[
        0
    ].status == "error"


async def test_offline_routing_never_constructs_http_client(monkeypatch, endpoint):
    monkeypatch.setattr(
        RoutingConfig,
        "load",
        lambda: RoutingConfig(enabled=True, endpoints={"personal": endpoint}),
    )
    monkeypatch.setattr(
        preflight, "HermesAPI", lambda _: pytest.fail("offline HTTP request")
    )
    checks = await preflight.routing_checks(offline=True)
    assert any("--offline" in c.detail for c in checks)


def test_doctor_respects_custom_home_and_offline(monkeypatch, tmp_path):
    home = tmp_path / "named-profile"
    plugin = home / "plugins/hfp-phone/plugin.yaml"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("name: hfp-phone")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong"))
    monkeypatch.setenv("HFP_MCP_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(
        diagnostics,
        "_fetch_daemon_health",
        lambda _: pytest.fail("offline HTTP request"),
    )
    monkeypatch.setattr(diagnostics, "_command_ok", lambda *args: (True, "active"))
    monkeypatch.setattr(RoutingConfig, "load", lambda: RoutingConfig())
    result = diagnostics.run_doctor(RuntimeConfig(), hermes_home=home, offline=True)
    assert next(c for c in result if c.name == "hermes-plugin").status == "ok"


def host_module():
    spec = importlib.util.spec_from_file_location(
        "host_config", Path(__file__).parents[1] / "setup/host_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_backup_preserves_content_absence_and_metadata_on_rerun(tmp_path):
    module = host_module()
    original, absent = tmp_path / "existing.env", tmp_path / "absent"
    original.write_text("secret=synthetic")
    original.chmod(0o640)
    snapshot = module.backup([original, absent], tmp_path / "backups")
    data = json.loads((snapshot / "manifest.json").read_text())["files"]
    assert data[0]["mode"] == 0o640
    assert data[1] == {"path": str(absent), "existed": False}
    saved = snapshot / data[0]["file"]
    assert saved.read_text() == original.read_text()
    assert saved.stat().st_mode & 0o777 == 0o600
    assert snapshot.stat().st_mode & 0o777 == 0o700
    original.write_text("new")
    second = module.backup([original], tmp_path / "backups")
    assert second != snapshot and saved.read_text() == "secret=synthetic"


def test_host_backup_rejects_symlinks(tmp_path):
    link = tmp_path / "symlink"
    link.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="non-regular"):
        host_module().backup([link], tmp_path / "backups")


def test_host_preflight_missing_prerequisite_does_not_write(monkeypatch, tmp_path):
    module = host_module()
    monkeypatch.setattr(
        module.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_dir=str(tmp_path)),
    )
    monkeypatch.setattr(module.Path, "is_dir", lambda _: True)
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="apt-get"):
        module.preflight(tmp_path, "test")
    assert list(tmp_path.iterdir()) == before
