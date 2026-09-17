from hfp_mcp import enrollment


def test_repairing_configured_phone_claims_enrollment_window(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from hfp_mcp.bluez.agent import HFPAgent

    monkeypatch.setattr(enrollment, "gate_path", lambda: tmp_path / "gate.json")
    enrollment.open_gate(30)
    agent = SimpleNamespace(_authorize=None, _configured_address="AA:BB:CC:DD:EE:FF")
    HFPAgent._require_authorized(agent, "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
    assert enrollment.enrollment_candidate() == "AA:BB:CC:DD:EE:FF"


def test_enrollment_gate_is_time_bounded_and_first_device_wins(monkeypatch, tmp_path):
    path = tmp_path / "enrollment.json"
    now = [1000.0]
    monkeypatch.setattr(enrollment, "gate_path", lambda: path)
    monkeypatch.setattr(enrollment.time, "time", lambda: now[0])

    enrollment.open_gate(30)

    assert enrollment.authorize_enrollment_address("AA:BB:CC:DD:EE:FF") is True
    assert enrollment.enrollment_candidate() == "AA:BB:CC:DD:EE:FF"
    assert enrollment.authorize_enrollment_address("11:22:33:44:55:66") is False
    assert path.stat().st_mode & 0o077 == 0

    now[0] = 1031.0
    assert enrollment.authorize_enrollment_address("AA:BB:CC:DD:EE:FF") is False
    assert not path.exists()


def test_service_env_phone_update_is_atomic_and_deduplicates(tmp_path):
    path = tmp_path / "hfp-mcp.env"
    path.write_text(
        "HFP_MCP_HOST=127.0.0.1\n"
        "HFP_PHONE_ADDRESS=11:11:11:11:11:11\n"
        "HFP_PHONE_ADDRESS=22:22:22:22:22:22\n",
        encoding="utf-8",
    )

    enrollment.set_phone_in_service_env(path, "aa:bb:cc:dd:ee:ff")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count("HFP_PHONE_ADDRESS=AA:BB:CC:DD:EE:FF") == 1
    assert lines.count("HFP_PHONE_DEFAULT_ADDRESS=AA:BB:CC:DD:EE:FF") == 1
    assert path.stat().st_mode & 0o077 == 0
