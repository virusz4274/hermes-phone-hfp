import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "install_hermes", Path(__file__).resolve().parents[1] / "setup/install_hermes.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(autouse=True)
def isolated_config_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HFP_MCP_ENV_FILE", str(tmp_path / ".config/hfp-mcp.env"))
    monkeypatch.delenv("HFP_MCP_CONFIG", raising=False)


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("name: hfp-phone")
    (source / "__init__.py").write_text("# current plugin")
    return source


def test_install_requires_existing_profile_and_preserves_settings_and_backup(
    tmp_path, source
):
    home = tmp_path / "profile"
    with pytest.raises(ValueError):
        installer.install_plugin(home, source)
    assert not home.exists()
    home.mkdir()
    previous = {
        "model": {"default": "existing"},
        "plugins": {
            "enabled": ["hfp-call-awareness", "other"],
            "disabled": ["hfp-phone", "unrelated"],
            "entries": {
                "hfp-phone": {"config": {"old_adapter": True}},
                "other": {"keep": True},
            },
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(previous))
    for name in ("hfp-phone", "hfp-call-awareness", "hfp-phone.backup-123"):
        old = home / "plugins" / name
        old.mkdir(parents=True)
        (old / "plugin.yaml").write_text("name: " + name)
    installer.install_plugin(home, source)
    current = yaml.safe_load((home / "config.yaml").read_text())
    assert current["model"] == previous["model"]
    assert current["plugins"]["enabled"] == ["other", "hfp-phone"]
    assert current["plugins"]["disabled"] == ["unrelated"]
    assert current["plugins"]["entries"] == {
        "other": {"keep": True},
        "hfp-phone": {"allow_tool_override": False},
    }
    backups = list((home / "hfp-phone-backups").iterdir())
    assert len(backups) == 1
    assert yaml.safe_load((backups[0] / "config.yaml").read_text()) == previous
    assert (backups[0] / "hfp-call-awareness/plugin.yaml").is_file()
    assert (backups[0] / "hfp-phone.backup-123/plugin.yaml").is_file()
    assert [p.name for p in (home / "plugins").iterdir()] == ["hfp-phone"]


def test_clean_install_and_reinstall_have_one_plugin_and_keep_caller_data(
    tmp_path, source
):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("model: existing\n")
    notes = home / "hfp-phone" / "callers.sqlite3"
    notes.parent.mkdir()
    notes.write_bytes(b"caller data")
    for _ in range(2):
        installer.install_plugin(home, source)
        config = yaml.safe_load((home / "config.yaml").read_text())
        assert config["plugins"]["enabled"] == ["hfp-phone"]
        assert "hfp-call-awareness" not in str(config)
        assert [p.name for p in (home / "plugins").iterdir()] == ["hfp-phone"]
        assert notes.read_bytes() == b"caller data"
        assert (home / "config.yaml").stat().st_mode & 0o077 == 0


def test_install_restores_plugins_if_configuration_write_fails(
    tmp_path, source, monkeypatch
):
    home = tmp_path / "profile"
    old = home / "plugins" / "hfp-phone"
    old.mkdir(parents=True)
    (old / "__init__.py").write_text("original")
    config = home / "config.yaml"
    config.write_text("model: existing\n")

    def fail_replace(*args):
        raise OSError("simulated config write failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        installer.install_plugin(home, source)
    assert (old / "__init__.py").read_text() == "original"
    assert config.read_text() == "model: existing\n"


def test_launcher_configures_with_selected_interpreter(tmp_path, monkeypatch):
    home = tmp_path / "profile with spaces"
    home.mkdir()
    (home / "config.yaml").write_text("model: existing\n")
    monkeypatch.setattr(
        sys,
        "argv",
        ["install_hermes.py", "--home", str(home), "--python", sys.executable],
    )
    real_run = subprocess.run
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        # Exercise the actual configuration subprocess without installing packages.
        if command[1] == "-c":
            return real_run(command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.main()
    assert all(command[0] == sys.executable for command in commands)
    assert len(commands) == 2
    assert "--no-deps" not in commands[0]
    assert (home / "plugins/hfp-phone/__init__.py").is_file()
    assert (tmp_path / ".config/hfp-mcp/config.yaml").is_file()


def test_path_setup_creates_no_caller_grants_and_preserves_credentials(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: existing\n")
    (home / ".env").write_text("API_SERVER_KEY=keep-api-secret\n")
    service_env = tmp_path / "daemon.env"
    service_env.write_text("HFP_GEMINI_API_KEY=keep-gemini-secret\n")
    example = Path(__file__).resolve().parents[1] / "setup/phone.example.yaml"
    selected = installer.configure_phone(home, example, service_env=service_env)
    config = yaml.safe_load(selected.read_text())
    assert config["phone"]["numbers"] == {}
    assert "default" not in config["phone"]
    assert "# numbers:" in selected.read_text()
    assert f'HFP_MCP_CONFIG="{selected}"' in service_env.read_text()
    assert f'HFP_MCP_CONFIG="{selected}"' in (home / ".env").read_text()
    assert "keep-api-secret" in (home / ".env").read_text()
    assert "keep-gemini-secret" in service_env.read_text()
    before = {p: p.read_bytes() for p in [selected, service_env, home / ".env"]}
    installer.configure_phone(home, example, service_env=service_env)
    assert all(p.read_bytes() == content for p, content in before.items())
    assert all(p.stat().st_mode & 0o077 == 0 for p in before)


def test_path_setup_respects_custom_yaml_and_rejects_conflicting_paths(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    custom = tmp_path / "custom path" / "calls.yaml"
    custom.parent.mkdir()
    custom.write_text("phone:\n  version: 1\n  numbers: {}\n")
    service_env = tmp_path / "daemon.env"
    service_env.write_text(f'HFP_MCP_CONFIG="{custom}"\n')
    example = Path(__file__).resolve().parents[1] / "setup/phone.example.yaml"
    original = custom.read_bytes()
    assert installer.configure_phone(home, example, service_env=service_env) == custom
    assert custom.read_bytes() == original
    (home / ".env").write_text(f'HFP_MCP_CONFIG="{tmp_path / "other.yaml"}"\n')
    before = service_env.read_bytes()
    with pytest.raises(ValueError, match="different routing paths"):
        installer.configure_phone(home, example, service_env=service_env)
    assert service_env.read_bytes() == before
    installer.configure_phone(
        home, example, phone_config=custom, service_env=service_env
    )
    assert f'HFP_MCP_CONFIG="{custom}"' in (home / ".env").read_text()
    assert custom.read_bytes() == original
