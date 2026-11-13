"""Deployment boundaries use temporary files, fake package builds and fake services."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from setup import runtime, update


def record(home=None):
    return {
        "format": 1, "base_python": "/usr/bin/python3.13", "python_version": "3.13.5",
        "extras": ["daemon", "gemini-live"], "hermes_home": str(home) if home else None,
        "hermes_launcher": "/bin/true" if home else None,
        "git_commit": "old", "git_dirty": False, "wheel_sha256": "a" * 64,
    }


def old_environment(repo, metadata):
    old = repo / ".venv-old"
    (old / "bin").mkdir(parents=True)
    (old / "bin/python").symlink_to(sys.executable)
    (old / runtime.RECORD).write_text(json.dumps(metadata))
    (repo / ".venv").symlink_to(old.name)
    return old


def contents(root):
    return {str(p.relative_to(root)): (p.readlink().as_posix() if p.is_symlink() else p.read_bytes())
            for p in root.rglob("*") if p.is_symlink() or p.is_file()}


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: keep\n")
    (home / ".env").write_text("TEST_SECRET=preserve\n")
    scripts = home / "scripts"
    scripts.mkdir()
    (scripts / "hfp-phone-job.py").write_text("# preserved scheduled job\n")
    (home / "workflows.sqlite3").write_bytes(b"preserved caller/workflow data")
    old = old_environment(repo, record(home))
    states = {runtime.DAEMON_UNIT: True, runtime.GATEWAY_UNIT: True}
    commands = []
    failure = {"step": None}

    def run(command, **kwargs):
        command = [str(part) for part in command]
        commands.append(command)
        normalized = [part for part in command if part != "-I"]
        if normalized[1:3] == ["-m", "venv"]:
            environment = Path(command[-1])
            (environment / "bin").mkdir()
            (environment / "bin/python").symlink_to(sys.executable)
        elif normalized[1:4] == ["-m", "pip", "wheel"]:
            wheels = Path(command[command.index("--wheel-dir") + 1])
            wheels.mkdir()
            (wheels / "hfp_mcp-0.1.1rc1-py3-none-any.whl").write_bytes(b"application wheel")
        elif normalized[1:4] == ["-m", "pip", "install"] and failure["step"] == "dependencies":
            raise subprocess.CalledProcessError(1, command)
        elif command[-2:] == ["gateway", "stop"]:
            if failure["step"] == "busy":
                raise subprocess.CalledProcessError(75, command)
            states[runtime.GATEWAY_UNIT] = False
        elif command[-2:] == ["gateway", "start"]:
            if failure["step"] == "start":
                raise subprocess.CalledProcessError(1, command)
            states[runtime.GATEWAY_UNIT] = True
        elif str(repo / "setup/install_hermes.py") in command and failure["step"] == "plugin":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def service(action, unit):
        commands.append(["service", action, unit])
        states[unit] = action == "start"

    def cleanup(command, **kwargs):
        assert command[:3] == ["systemctl", "--user", "stop"]
        service("stop", command[3])
        return subprocess.CompletedProcess(command, 0)

    def probe(python, mode, **kwargs):
        if mode == "idle":
            return {"idle": failure["step"] != "call", "tasks_unavailable": False}
        return {"ok": True}

    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "probe_python", lambda *a, **kw: {
        "base_python": "/usr/bin/python3.13", "python_version": "3.13.5",
    })
    monkeypatch.setattr(runtime, "smoke_environment", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "check_dependencies", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "source_revision", lambda _: ("new", False))
    monkeypatch.setattr(runtime, "app_probe", probe)
    monkeypatch.setattr(runtime, "service_state", lambda unit: states[unit])
    monkeypatch.setattr(runtime, "systemctl", service)
    monkeypatch.setattr(update, "hermes_running", lambda _: states[runtime.GATEWAY_UNIT])
    monkeypatch.setattr(update.subprocess, "run", cleanup)
    return repo, home, old, states, commands, failure


def test_fresh_build_records_wheel_and_activates_without_renaming(deployment):
    repo, home, old, states, commands, failure = deployment
    (repo / ".venv").unlink()
    runtime.validate_fresh(repo)
    info = {"base_python": "/usr/bin/python3.13", "python_version": "3.13.5"}
    candidate = runtime.build_environment(repo, info, ["daemon"], str(home), "/bin/true")
    before = (candidate / "bin/python").readlink()
    runtime.activate(repo, candidate)
    target, installed = runtime.read_installation(repo)
    assert target == candidate and candidate.exists() and old.exists()
    assert installed["extras"] == ["daemon"]
    assert installed["wheel_sha256"] == hashlib.sha256(b"application wheel").hexdigest()
    assert installed["git_commit"] == "new"
    assert (candidate / "bin/python").readlink() == before
    assert "--system-site-packages" in commands[0]
    assert not any("-e" in command for command in commands)
    assert all(command[1] == "-I" for command in commands if "pip" in command)


def test_repeat_update_retains_python_gemini_data_and_previous_venvs(deployment):
    repo, home, old, states, commands, failure = deployment
    saved = contents(home)
    update.perform_update(repo)
    first, first_record = runtime.read_installation(repo)
    assert first != old and old.exists()
    assert first_record["extras"] == ["daemon", "gemini-live"]
    assert first_record["base_python"] == "/usr/bin/python3.13"
    assert all(states.values())
    assert contents(home) == saved
    assert commands.index(["service", "stop", runtime.DAEMON_UNIT]) > next(
        i for i, c in enumerate(commands) if "pip" in c and "install" in c)
    update.perform_update(repo)
    second, _ = runtime.read_installation(repo)
    assert second != first and first.exists() and old.exists()
    assert contents(home) == saved


def test_check_is_read_only_and_never_bootstraps_hermes_or_builds(deployment):
    repo, home, old, states, commands, failure = deployment
    before = contents(repo)
    saved = contents(home)
    update.perform_update(repo, check=True)
    assert contents(repo) == before
    assert contents(home) == saved
    assert not commands
    assert all(states.values())
    assert not (repo / ".hfp-update.lock").exists()


def test_python_minor_change_requires_explicit_choice(deployment, monkeypatch):
    repo, home, old, states, commands, failure = deployment
    monkeypatch.setattr(runtime, "probe_python", lambda *a, **kw: {
        "base_python": "/host/python3.12", "python_version": "3.12.9",
    })
    with pytest.raises(ValueError, match="minor version changed"):
        update.perform_update(repo)
    assert not commands and (repo / ".venv").resolve() == old
    update.perform_update(repo, python="/host/python3.12")
    _, installed = runtime.read_installation(repo)
    assert installed["base_python"] == "/host/python3.12"
    assert installed["python_version"] == "3.12.9"


@pytest.mark.parametrize("step", ["dependencies", "busy", "call"])
def test_preparation_or_activity_refusal_leaves_services_and_selection(deployment, step):
    repo, home, old, states, commands, failure = deployment
    failure["step"] = step
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        update.perform_update(repo)
    assert (repo / ".venv").resolve() == old and all(states.values())
    assert not any(c[:2] == ["service", "stop"] for c in commands)
    if step == "dependencies":
        assert list(repo.glob(".venv-*")) == [old]


@pytest.mark.parametrize("step", ["plugin", "start"])
def test_cutover_failure_stops_services_and_retains_recovery_env(deployment, step, capsys):
    repo, home, old, states, commands, failure = deployment
    saved = contents(home)
    failure["step"] = step
    with pytest.raises(subprocess.CalledProcessError):
        update.perform_update(repo)
    assert not any(states.values())
    assert old.exists() and contents(home) == saved
    assert "Previous environment:" in capsys.readouterr().err
    selected = (repo / ".venv").resolve()
    assert (selected == old) == (step == "plugin")


def test_previously_stopped_services_stay_stopped(deployment):
    repo, home, old, states, commands, failure = deployment
    states.update({unit: False for unit in states})
    update.perform_update(repo)
    assert not any(states.values())
    assert not any(c[:2] == ["service", "start"] or c[-2:] == ["gateway", "start"] for c in commands)


def test_foreground_gateway_is_refused_before_preparing(deployment, monkeypatch):
    repo, home, old, states, commands, failure = deployment
    states[runtime.GATEWAY_UNIT] = False
    monkeypatch.setattr(update, "hermes_running", lambda _: True)
    with pytest.raises(ValueError, match="foreground/custom"):
        update.perform_update(repo)
    assert not commands and (repo / ".venv").resolve() == old


def test_invalid_bridge_configuration_leaves_running_services_untouched(deployment, monkeypatch):
    repo, home, old, states, commands, failure = deployment
    original = runtime.app_probe
    def probe(python, mode, **kwargs):
        if mode == "bridge-config":
            raise ValueError("missing bridge credential")
        return original(python, mode, **kwargs)
    monkeypatch.setattr(runtime, "app_probe", probe)
    with pytest.raises(ValueError, match="bridge credential"):
        update.perform_update(repo)
    assert all(states.values()) and (repo / ".venv").resolve() == old
    assert not any(c[-2:] == ["gateway", "stop"] for c in commands)


def test_daemon_start_failure_stops_both_services(deployment, monkeypatch):
    repo, home, old, states, commands, failure = deployment
    original = runtime.systemctl
    def service(action, unit):
        if action == "start" and unit == runtime.DAEMON_UNIT:
            raise subprocess.CalledProcessError(1, ["systemctl", action, unit])
        original(action, unit)
    monkeypatch.setattr(runtime, "systemctl", service)
    with pytest.raises(subprocess.CalledProcessError):
        update.perform_update(repo)
    assert not any(states.values()) and old.exists()


def test_broken_venv_can_be_diagnosed_without_app_imports(deployment):
    repo, home, old, states, commands, failure = deployment
    (old / "bin/python").unlink()
    with pytest.raises(ValueError, match="Broken daemon venv"):
        update.perform_update(repo, check=True)
    assert not commands and all(states.values())


def test_stopped_broken_venv_can_be_rebuilt_from_its_record(deployment):
    repo, home, old, states, commands, failure = deployment
    states.update({unit: False for unit in states})
    (old / "bin/python").unlink()
    update.perform_update(repo)
    candidate, _ = runtime.read_installation(repo)
    assert candidate != old and (candidate / "bin/python").is_file()
    assert not any(states.values())


def test_legacy_layout_is_not_migrated(tmp_path):
    (tmp_path / ".venv").mkdir()
    for operation in (runtime.validate_fresh, runtime.read_installation):
        with pytest.raises(ValueError, match="fresh|Legacy"):
            operation(tmp_path)
    assert (tmp_path / ".venv").is_dir()


def test_nested_update_lock_refuses_second_writer(tmp_path):
    with runtime.update_lock(tmp_path):
        with pytest.raises(ValueError, match="Another installation"):
            with runtime.update_lock(tmp_path):
                pytest.fail("second writer admitted")


def test_missing_native_bindings_fail_before_build(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/host/python")
    def probe(command, **kwargs):
        if "sys._base_executable" in command[-1]:
            return {"base_python": "/host/python", "python_version": "3.13.5"}
        raise ValueError("missing gi")
    monkeypatch.setattr(runtime, "json_command", probe)
    with pytest.raises(ValueError, match="D-Bus/GLib"):
        runtime.probe_python("/host/python", native=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("version", [(3, 10), (3, 14)])
def test_daemon_python_range_is_distinct_from_portable_plugin(version):
    with pytest.raises(ValueError, match="daemon runtime"):
        runtime.validate_version(version)


def test_candidate_readiness_failure_is_reported(deployment, monkeypatch):
    repo, home, old, states, commands, failure = deployment
    original = update.wait_ready
    def fail_health(python, mode, **kwargs):
        if mode == "daemon":
            raise ValueError("not ready")
        return {"idle": True, "tasks_unavailable": False}
    monkeypatch.setattr(runtime, "app_probe", fail_health)
    monkeypatch.setattr(update, "wait_ready", lambda *a, **kw: original(*a, **kw, timeout=0))
    with pytest.raises(ValueError, match="readiness failed"):
        update.perform_update(repo)
    assert not any(states.values()) and old.exists()


def test_dependency_validation_ignores_unrelated_distro_packages():
    packages = {
        "hfp-mcp": SimpleNamespace(version="0.1", requires=['binding>=1,<2; extra == "daemon"']),
        "binding": SimpleNamespace(version="1.5", requires=None),
        "unrelated-distro-tool": SimpleNamespace(version="1", requires=["missing-package"]),
    }
    assert runtime.dependency_errors(["daemon"], packages.__getitem__) == []


def test_dependency_validation_checks_extras_transitive_versions_and_missing_packages():
    from importlib.metadata import PackageNotFoundError
    packages = {
        "hfp-mcp": SimpleNamespace(version="0.1", requires=['binding[audio]>=1,<2; extra == "daemon"']),
        "binding": SimpleNamespace(version="3", requires=['missing>=1; extra == "audio"']),
    }
    def lookup(name):
        if name not in packages:
            raise PackageNotFoundError(name)
        return packages[name]
    assert runtime.dependency_errors([], lookup) == []
    errors = runtime.dependency_errors(["daemon"], lookup)
    assert any("found 3" in error for error in errors)
    assert any("missing" in error for error in errors)


@pytest.mark.parametrize("call_state,expected", [("idle", True), ("incoming", False), ("active", False)])
def test_idle_probe_checks_raw_calls_even_with_phone_controller_disabled(monkeypatch, capsys, call_state, expected):
    from hfp_mcp import phone_cli
    from hfp_mcp.settings import RuntimeConfig
    from hfp_mcp.routing import RoutingConfig
    monkeypatch.setattr(RuntimeConfig, "load", lambda: SimpleNamespace())
    monkeypatch.setattr(RoutingConfig, "load", lambda: SimpleNamespace())
    responses = {
        "/v1/phone": {"enabled": False, "native_phone_tasks": [], "scheduled_callbacks": []},
        "/v1/state": {"call": {"state": call_state}},
    }
    monkeypatch.setattr(phone_cli, "control", responses.__getitem__)
    monkeypatch.setattr(sys, "argv", ["probe", "idle", "default"])
    exec(runtime.APP_PROBE, {})
    assert json.loads(capsys.readouterr().out)["idle"] is expected


def test_malformed_record_is_diagnosed(tmp_path):
    old = old_environment(tmp_path, [])
    with pytest.raises(ValueError, match="Invalid installation record"):
        runtime.read_installation(tmp_path)
    assert old.exists()
