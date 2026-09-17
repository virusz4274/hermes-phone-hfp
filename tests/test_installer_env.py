"""Tests for installer-generated hfp-mcp service defaults."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_HELPER_PATH = Path(__file__).resolve().parents[1] / "setup" / "render_hfp_env.py"
_SPEC = importlib.util.spec_from_file_location("render_hfp_env", _HELPER_PATH)
render_hfp_env = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(render_hfp_env)


def test_choose_public_host_prefers_non_loopback_ipv4():
    host = render_hfp_env.choose_public_host(
        "127.0.0.1 192.168.1.42 10.0.0.9",
        "hfp-pi.local",
    )

    assert host == "192.168.1.42"


def test_choose_public_host_falls_back_to_fqdn_then_mdns_name():
    assert render_hfp_env.choose_public_host("", "hfp-pi.local") == "hfp-pi.local"
    assert render_hfp_env.choose_public_host("", "localhost") == "raspberrypi.local"


def test_render_env_uses_authenticated_loopback_defaults():
    content = render_hfp_env.render_env("192.168.1.42", token="t" * 48)

    assert "HFP_MCP_HOST=127.0.0.1" in content
    assert "HFP_MCP_PORT=8000" in content
    assert "HFP_MCP_BEARER_TOKEN=" + ("t" * 48) in content
    assert "HFP_MCP_DAEMON_URL=http://127.0.0.1:8000/mcp" in content
    assert "HFP_PHONE_MCP_TOKEN" not in content
    assert "HFP_PHONE_MCP_URL" not in content
    assert "HFP_PHONE_STATUS_URL" not in content
    assert "HFP_MCP_TRUSTED_TLS_PROXY=true" in content
    assert "HFP_HERMES_API_KEY=" in content
    assert "HFP_HERMES_BRIDGE_KEY=" in content
    assert "HFP_PHONE_VOICE_MODE" not in content
    assert "HFP_PHONE_AUTO_RECONNECT" not in content
    assert "HFP_PHONE_ADMIN_APPROVAL_BYPASS" not in content
    assert "#HFP_GEMINI_LIVE_VOICE=Autonoe" in content
    assert "\nHFP_GEMINI_LIVE_VOICE=" not in content


def test_installer_includes_gemini_live_optional_dependencies():
    install_script = (
        Path(__file__).resolve().parents[1] / "setup" / "install.sh"
    ).read_text()

    assert "[daemon,gemini-live]" in install_script
    assert '"$REPO_DIR[daemon]"' in install_script
    assert "Including Gemini Live optional dependencies" in install_script


def test_fresh_environment_stays_canonical_when_installer_runs_again():
    content = render_hfp_env.render_env("phone.example", token="t" * 48)
    rerendered, changed = render_hfp_env.migrate_env_content(content, "phone.example")
    assert not changed
    assert rerendered == content
    for obsolete in ("HFP_PHONE_MCP_TOKEN", "HFP_PHONE_MCP_URL", "HFP_PHONE_STATUS_URL"):
        assert obsolete not in rerendered


def test_unversioned_environment_does_not_gain_obsolete_settings():
    rendered, _ = render_hfp_env.migrate_env_content(
        "HFP_MCP_PORT=9000\n", "phone.example", token="t" * 48
    )
    assert "HFP_MCP_DAEMON_URL=http://127.0.0.1:9000/mcp" in rendered
    for obsolete in ("HFP_PHONE_MCP_TOKEN", "HFP_PHONE_MCP_URL", "HFP_PHONE_STATUS_URL"):
        assert obsolete not in rendered


def test_migrate_env_content_adds_missing_newer_defaults():
    content = 'HFP_MCP_OPTS="--port 9000"\n'

    migrated, changed = render_hfp_env.migrate_env_content(
        content, "192.168.1.42", token="x" * 48
    )

    assert changed is True
    assert "HFP_MCP_PORT=9000" in migrated
    assert 'HFP_MCP_OPTS=""' in migrated
    assert "HFP_MCP_HOST=127.0.0.1" in migrated
    assert "HFP_MCP_BEARER_TOKEN=" + ("x" * 48) in migrated


def test_migrate_env_content_preserves_provider_secrets_but_removes_insecure_flags():
    content = (
        'HFP_MCP_OPTS="--port 9000 --status-port 9001 '
        '--audio-host 0.0.0.0 --audio-public-host phone-pi.local"\n'
        'HFP_GEMINI_API_KEY="secret"\n'
    )

    migrated, changed = render_hfp_env.migrate_env_content(
        content, "192.168.1.42", token="y" * 48
    )

    assert changed is True
    assert 'HFP_GEMINI_API_KEY="secret"' in migrated
    assert 'HFP_MCP_OPTS=""' in migrated
    assert "--audio-host" not in next(
        line for line in migrated.splitlines() if line.startswith("HFP_MCP_OPTS=")
    )


def test_migration_preserves_coherent_direct_tls_operator_config():
    content = (
        "HFP_MCP_HOST=0.0.0.0\n"
        "HFP_MCP_PORT=8443\n"
        "HFP_MCP_PUBLIC_HOST=phone.example.lan\n"
        "HFP_MCP_TLS_CERT=/etc/hfp/cert.pem\n"
        "HFP_MCP_TLS_KEY=/etc/hfp/key.pem\n"
        'HFP_MCP_OPTS="--allowed-host phone.example.lan:8443"\n'
    )

    migrated, changed = render_hfp_env.migrate_env_content(
        content, "fallback.example", token="q" * 48
    )

    assert changed is True
    assert "HFP_MCP_HOST=0.0.0.0" in migrated
    assert "HFP_MCP_PUBLIC_HOST=phone.example.lan" in migrated
    assert 'HFP_MCP_OPTS="--allowed-host phone.example.lan:8443"' in migrated
    assert "HFP_MCP_DAEMON_URL=https://phone.example.lan:8443/mcp" in migrated

    second, changed_again = render_hfp_env.migrate_env_content(
        migrated, "different.example"
    )
    assert changed_again is False
    assert second == migrated


def test_sync_token_file_creates_private_stdio_proxy_credential(tmp_path):
    env_path = tmp_path / "hfp-mcp.env"
    token_path = tmp_path / "state" / "control.token"
    token = "z" * 48
    env_path.write_text(f"HFP_MCP_BEARER_TOKEN={token}\n", encoding="utf-8")

    assert render_hfp_env.sync_token_file(env_path, token_path) == token
    assert token_path.read_text(encoding="utf-8").strip() == token
    assert token_path.stat().st_mode & 0o077 == 0


def test_v2_rerun_reconciles_daemon_and_hermes_tokens():
    daemon_token = "d" * 48
    content = (
        "# hfp-mcp migration-version=2\n"
        f"HFP_MCP_BEARER_TOKEN={daemon_token}\n"
        f"HFP_PHONE_MCP_TOKEN={'h' * 48}\n"
    )

    migrated, changed = render_hfp_env.migrate_env_content(
        content, "phone.example.lan"
    )

    assert changed is True
    assert f"HFP_MCP_BEARER_TOKEN={daemon_token}" in migrated
    assert f"HFP_PHONE_MCP_TOKEN={daemon_token}" in migrated


def test_bluetooth_installer_does_not_mutate_or_restart_hermes():
    script = (Path(__file__).resolve().parents[1] / "setup/install.sh").read_text()
    assert "try-restart hermes-gateway.service" not in script
    assert "plugins enable" not in script
    assert "config set" not in script
    assert "--with-gemini" in script


def test_installer_supports_wireplumber_04_and_05_formats():
    root = Path(__file__).resolve().parents[1]
    script = (root / "setup/install.sh").read_text()

    assert "dpkg --compare-versions" in script
    assert "/etc/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua" in script
    assert "/etc/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf" in script
    assert (root / "setup/90-hfp-mcp.lua").exists()
    lua_policy = (root / "setup/90-hfp-mcp.lua").read_text()
    assert 'bluez_monitor.properties["bluez5.roles"] =' in lua_policy
    assert "bluez_monitor.properties = {" not in lua_policy


def test_installer_renders_dbus_policy_for_the_exact_service_account():
    root = Path(__file__).resolve().parents[1]
    script = (root / "setup/install.sh").read_text()
    policy = (root / "setup/bluetooth-policy.conf").read_text()

    assert 'policy user="__SERVICE_USER__"' in policy
    assert 'sed "s/__SERVICE_USER__/$SERVICE_USER/g"' in script


def test_installer_verifies_daemon_liveness_and_user_local_hermes():
    script = (
        Path(__file__).resolve().parents[1] / "setup/install.sh"
    ).read_text()

    assert 'hfp-mcp" wait-live --timeout 90' in script
    assert "setup/install_hermes.py" in script
    assert "HFP_PHONE_DEFAULT_ADDRESS" not in script  # atomic helper owns both keys
