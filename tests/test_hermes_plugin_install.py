import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
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
    root = Path(__file__).resolve().parents[1]
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, tmp_path / name)
    package = tmp_path / "src/hfp_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# bundled package\n")
    (package / "hermes_bridge.py").write_text("def register(ctx): pass\n")
    (package / "phone_workflow_runner.py").write_text("def main(*args): pass\n")
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("name: hfp-phone")
    (source / "__init__.py").write_text("# current plugin")
    return source


@pytest.fixture(autouse=True)
def isolated_pm(monkeypatch):
    # The real PM integration is exercised against a disposable Hermes home.
    # Unit tests model its publication/selection boundaries without touching it.
    def publish(home, staged):
        destination = home / "plugins/hfp-phone"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(staged), str(destination))

    def select(home, plugins):
        path = home / "config.yaml"
        config = yaml.safe_load(path.read_text()) or {}
        section = config.setdefault("plugins", {})
        section["enabled"] = plugins["enabled"]
        if "disabled" in plugins:
            section["disabled"] = plugins["disabled"]
        path.write_text(yaml.safe_dump(config))

    monkeypatch.setattr(installer, "publish_plugin", publish)
    monkeypatch.setattr(installer, "select_plugin", select)


def test_staged_plugin_declares_and_bundles_the_portable_package(tmp_path, source):
    staged = tmp_path / "staged"
    installer.stage_plugin(source, staged)
    project = tomllib.loads((staged / "pyproject.toml").read_text())
    assert project["project"]["name"] == "hfp-mcp"
    assert project["project"]["requires-python"] == ">=3.11,<3.15"
    assert "build-system" in project
    assert "optional-dependencies" not in project["project"]
    assert "scripts" not in project["project"]
    assert not any("dbus" in req or "PyGObject" in req or "google-genai" in req
                   for req in project["project"]["dependencies"])
    assert (staged / "src/hfp_mcp/hermes_bridge.py").is_file()
    assert (staged / "src/hfp_mcp/phone_workflow_runner.py").is_file()
    assert (staged / "LICENSE").is_file()


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


def test_dependency_failure_preserves_original_plugin_and_configuration(
    tmp_path, source, monkeypatch
):
    home = tmp_path / "profile"
    old = home / "plugins" / "hfp-phone"
    old.mkdir(parents=True)
    (old / "__init__.py").write_text("original")
    config = home / "config.yaml"
    config.write_text("model: existing\n")

    def fail_publish(*args):
        raise RuntimeError("simulated dependency conflict")

    monkeypatch.setattr(installer, "publish_plugin", fail_publish)
    with pytest.raises(RuntimeError, match="dependency conflict"):
        installer.install_plugin(home, source)
    assert (old / "__init__.py").read_text() == "original"
    assert config.read_text() == "model: existing\n"


def test_launcher_uses_the_published_managed_bootstrap(tmp_path, monkeypatch):
    home = tmp_path / "profile with spaces"
    home.mkdir()
    (home / "config.yaml").write_text("model: existing\n")
    monkeypatch.setattr(
        sys,
        "argv",
        ["install_hermes.py", "--home", str(home), "--hermes", "hermes"],
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["HERMES_HOME"] == str(home)
        if command[0] == "hermes":
            assert command[1:5] == ["--print-runtime-command", "--module", "stat", "--"]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([
                "/managed/python", "-I", "-c", "import sys, runpy; import hermes_bootstrap",
                *command[5:],
            ]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.main()
    assert len(commands) == 2
    assert commands[1][0:3] == ["/managed/python", "-I", "-c"]
    assert "import hermes_bootstrap" in commands[1][3]
    assert "runpy.run_path" in commands[1][3]
    assert commands[1][4:] == [str(installer.__file__), "--managed-child", "--home", str(home)]
    assert not (home / "plugins").exists()


def test_managed_command_executes_file_arguments_with_spaces(tmp_path):
    script = tmp_path / "script with spaces.py"
    script.write_text("import sys; print(repr(sys.argv))\n")
    # Exercise the complete adapter rather than shell-quoting a Python fragment.
    native = [sys.executable, "-I", "-c", "import sys, runpy; runpy.run_module('stat', run_name='__main__', alter_sys=True)",
              str(script), "argument with spaces"]
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=json.dumps(native)))
        command = installer.managed_script_command("hermes", script, ["argument with spaces"], env={})
    finally:
        monkeypatch.undo()
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    assert repr([str(script), "argument with spaces"]) in completed.stdout


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
