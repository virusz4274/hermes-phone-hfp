from hfp_mcp import diagnostics
from hfp_mcp.settings import RuntimeConfig


def test_enrollment_uses_daemon_agent_and_configures_exact_phone(monkeypatch, tmp_path):
    calls = []
    gate_events = []
    env_path = tmp_path / "hfp-mcp.env"
    env_path.write_text("HFP_MCP_HOST=127.0.0.1\n", encoding="utf-8")
    env_path.chmod(0o600)

    monkeypatch.setenv("HFP_MCP_ENV_FILE", str(env_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        diagnostics,
        "_command_ok",
        lambda *command: (calls.append(command) or (True, "")),
    )
    monkeypatch.setattr(diagnostics, "open_gate", lambda timeout: gate_events.append(("open", timeout)))
    monkeypatch.setattr(diagnostics, "close_gate", lambda: gate_events.append(("close", None)))
    monkeypatch.setattr(
        diagnostics, "_find_enrolled_hfp_phone", lambda: "AA:BB:CC:DD:EE:FF"
    )
    monotonic_values = iter((0.0, 1.0))
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(monotonic_values))

    assert diagnostics.enroll_main(["--timeout", "30"]) == 0
    assert ("bluetoothctl", "pairable", "on") in calls
    assert ("bluetoothctl", "discoverable", "on") in calls
    assert ("bluetoothctl", "agent", "on") not in calls
    assert gate_events == [("open", 30), ("close", None)]
    close_index = calls.index(("bluetoothctl", "scan", "off"))
    assert calls[close_index : close_index + 3] == [
        ("bluetoothctl", "scan", "off"),
        ("bluetoothctl", "discoverable", "off"),
        ("bluetoothctl", "pairable", "off"),
    ]
    assert ("bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF") in calls
    assert (
        "systemctl",
        "--user",
        "restart",
        "hfp-mcp.service",
    ) in calls
    content = env_path.read_text(encoding="utf-8")
    assert "HFP_PHONE_ADDRESS=AA:BB:CC:DD:EE:FF" in content
    assert "HFP_PHONE_DEFAULT_ADDRESS=AA:BB:CC:DD:EE:FF" in content


def test_replacement_enrollment_ignores_preexisting_sole_pairing(monkeypatch):
    monkeypatch.setattr(diagnostics, "enrollment_candidate", lambda: None)
    monkeypatch.setattr(
        diagnostics,
        "_paired_hfp_addresses",
        lambda: ["AA:BB:CC:DD:EE:FF"],
    )

    assert diagnostics._find_enrolled_hfp_phone() is None


def test_enrollment_selects_only_the_gate_candidate(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "enrollment_candidate",
        lambda: "11:22:33:44:55:66",
    )
    monkeypatch.setattr(
        diagnostics,
        "_paired_hfp_addresses",
        lambda: ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"],
    )

    assert diagnostics._find_enrolled_hfp_phone() == "11:22:33:44:55:66"


def test_wait_live_requires_service_and_http_health(monkeypatch):
    monkeypatch.setattr(
        diagnostics.RuntimeConfig,
        "load",
        lambda: RuntimeConfig(daemon_url="http://127.0.0.1:8000/mcp"),
    )
    monkeypatch.setattr(
        diagnostics,
        "_command_ok",
        lambda *command: (True, "active"),
    )
    monkeypatch.setattr(
        diagnostics,
        "_fetch_daemon_health",
        lambda _config: (True, "http://127.0.0.1:8000/healthz: bluez=profile_ready"),
    )

    assert diagnostics.wait_live_main(["--timeout", "1"]) == 0


def test_health_url_preserves_secure_daemon_origin():
    config = RuntimeConfig(daemon_url="https://phone.example.lan/mcp")

    assert diagnostics._daemon_health_url(config) == (
        "https://phone.example.lan/healthz"
    )
