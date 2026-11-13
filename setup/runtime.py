"""Host-Python daemon environments shared by installation and updates (stdlib only)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 13)
PYTHON_RANGE = f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1]}"
RECORD = "hfp-install.json"
DAEMON_UNIT = "hfp-mcp.service"
GATEWAY_UNIT = "hermes-gateway.service"


def validate_version(version):
    if not MIN_PYTHON <= tuple(version[:2]) <= MAX_PYTHON:
        raise ValueError(f"Supported daemon runtime: {PYTHON_RANGE}")


def run(command, **kwargs):
    """Keep build output on stderr so prepare's stdout is just the candidate path."""
    return subprocess.run([str(part) for part in command], check=True, stdout=sys.stderr, **kwargs)


def json_command(command, *, env=None, timeout=30):
    result = subprocess.run(
        [str(part) for part in command], env=env, timeout=timeout,
        capture_output=True, text=True,
    )
    if result.returncode:
        # Configuration and HTTP exceptions may contain credentials; do not echo them.
        raise ValueError(f"Runtime check failed (exit {result.returncode}); run the selected interpreter's diagnostics")
    return json.loads(result.stdout)


def probe_python(executable, *, native=False):
    resolved = shutil.which(str(Path(executable).expanduser()))
    if not resolved:
        raise ValueError(f"Python interpreter not found: {executable}; install it on the host first")
    code = (
        "import sys,json; print(json.dumps({'base_python':sys._base_executable,"
        "'python_version':'.'.join(map(str,sys.version_info[:3]))}))"
    )
    info = json_command([resolved, "-I", "-B", "-c", code])
    validate_version(tuple(map(int, info["python_version"].split("."))))
    # Probe the actual host base, not packages inherited from a caller's venv.
    info["base_python"] = str(Path(info["base_python"]).resolve())
    if native:
        code = (
            "import dbus,dbus.mainloop.glib,gi; from gi.repository import GLib; "
            "assert GLib.MainLoop; import json; print(json.dumps({'native':True}))"
        )
        try:
            json_command([info["base_python"], "-I", "-B", "-c", code])
        except ValueError as exc:
            raise ValueError(
                "D-Bus/GLib bindings are missing for the selected host Python. "
                "Install matching python3-dbus/python3-gi bindings or compatible native builds; "
                "do not copy compiled bindings from another Python minor version."
            ) from exc
    return info


def validate_home(home, launcher):
    if home is None:
        return None, None
    home = Path(home).expanduser().resolve()
    if not (home / "config.yaml").is_file():
        raise ValueError("--hermes-home must name an existing Hermes home")
    executable = shutil.which(str(Path(launcher).expanduser()))
    if not executable:
        raise ValueError("Managed Hermes launcher not found; select it with --hermes")
    return str(home), str(Path(executable).absolute())


def validate_fresh(repo):
    alias = repo / ".venv"
    if alias.exists() or alias.is_symlink():
        raise ValueError(
            "This installer requires a fresh checkout without .venv. "
            "Use setup/update.py for the new layout, or follow the clean-reinstall instructions "
            "for legacy installs. Configuration and data must be retained."
        )


def read_installation(repo):
    alias = repo / ".venv"
    if not alias.is_symlink():
        raise ValueError("Legacy or missing .venv layout; follow the clean-reinstall instructions")
    target = alias.resolve()
    if target.parent != repo.resolve() or not target.name.startswith(".venv-"):
        raise ValueError("The .venv link must select a .venv-<id> directory in this checkout")
    try:
        record = json.loads((target / RECORD).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Broken environment: installation record is missing or unreadable; reinstall") from exc
    if (not isinstance(record, dict) or record.get("format") != 1 or not isinstance(record.get("base_python"), str)
            or not isinstance(record.get("python_version"), str)
            or record.get("extras") not in (["daemon"], ["daemon", "gemini-live"])
            or not isinstance(record.get("wheel_sha256"), str)
            or not isinstance(record.get("hermes_home"), (str, type(None)))
            or not isinstance(record.get("hermes_launcher"), (str, type(None)))
            or bool(record.get("hermes_home")) != bool(record.get("hermes_launcher"))):
        raise ValueError("Invalid installation record; reinstall")
    return target, record


@contextmanager
def update_lock(repo):
    with (repo / ".hfp-update.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another installation or update is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def source_revision(repo):
    if not shutil.which("git"):
        return None, None
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    if result.returncode:
        return None, None
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return result.stdout.strip(), bool(dirty.stdout.strip())


def build_environment(repo, info, extras, home=None, launcher=None):
    """Build at the final path: console scripts and pyvenv.cfg refer to it."""
    candidate = Path(tempfile.mkdtemp(prefix=".venv-", dir=repo))
    try:
        run([info["base_python"], "-I", "-m", "venv", "--system-site-packages", candidate])
        python = candidate / "bin/python"
        wheels = candidate / "wheels"
        run([python, "-I", "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
             "--no-deps", "--wheel-dir", wheels, repo])
        artifacts = list(wheels.glob("hfp_mcp-*.whl"))
        if len(artifacts) != 1:
            raise ValueError("Expected exactly one hfp-mcp application wheel")
        wheel = artifacts[0]
        # Force only our application into this venv; distro native bindings remain
        # eligible to satisfy dependencies without being upgraded or uninstalled.
        run([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
             "--ignore-installed", "--no-deps", wheel])
        requirement = f"hfp-mcp[{','.join(extras)}] @ {wheel.as_uri()}"
        run([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", requirement])
        check_dependencies(python, extras)
        smoke_environment(python, gemini="gemini-live" in extras)
        revision, dirty = source_revision(repo)
        record = {
            "format": 1, **info, "extras": extras,
            "hermes_home": home, "hermes_launcher": launcher,
            "git_commit": revision, "git_dirty": dirty,
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
        path = candidate / RECORD
        path.write_text(json.dumps(record, indent=2) + "\n")
        path.chmod(0o600)
        return candidate
    except BaseException:
        shutil.rmtree(candidate)
        raise


def smoke_environment(python, *, gemini):
    code = (
        "import sys,json; from pathlib import Path; import dbus,dbus.mainloop.glib,gi; "
        "from gi.repository import GLib; import hfp_mcp, hfp_mcp.server; "
        "from hfp_mcp.audio.opening import outgoing_opening; "
        "assert Path(hfp_mcp.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); "
        "assert outgoing_opening().pcm; "
        + ("import google.genai,soxr; " if gemini else "")
        + "print(json.dumps({'smoke':True}))"
    )
    # Server imports and optional providers must never touch real user state.
    with tempfile.TemporaryDirectory(prefix="hfp-smoke-") as directory:
        env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": directory,
            "HFP_MCP_ENV_FILE": str(Path(directory) / "absent.env"),
            "HFP_MCP_CONFIG": str(Path(directory) / "absent.yaml"),
            "XDG_STATE_HOME": directory, "XDG_CACHE_HOME": directory,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        json_command([python, "-I", "-B", "-c", code], env=env)


def dependency_errors(extras, get_distribution=None):
    """Check our dependency closure, not unrelated distro tools visible via site.

    Executed only by a selected venv subprocess: importing this source module
    from the host updater still requires only the standard library.
    """
    from importlib.metadata import PackageNotFoundError, distribution
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    get_distribution = get_distribution or distribution
    pending = [("hfp-mcp", frozenset(extras))]
    seen, errors = set(), set()
    while pending:
        name, enabled = pending.pop()
        identity = (canonicalize_name(name), enabled)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            installed = get_distribution(name)
        except PackageNotFoundError:
            errors.add(f"{name} is missing")
            continue
        for spec in installed.requires or ():
            requirement = Requirement(spec)
            if requirement.marker and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in ("", *enabled)
            ):
                continue
            try:
                dependency = get_distribution(requirement.name)
            except PackageNotFoundError:
                errors.add(f"{name} requires missing {requirement.name}")
                continue
            if not requirement.specifier.contains(dependency.version, prereleases=True):
                errors.add(f"{name} requires {requirement.name}{requirement.specifier}; found {dependency.version}")
            pending.append((requirement.name, frozenset(requirement.extras)))
    return sorted(errors)


def check_dependencies(python, extras):
    code = ("import json,runpy,sys; tools=runpy.run_path(sys.argv[1]); "
            "print(json.dumps(tools['dependency_errors'](sys.argv[2:])))")
    errors = json_command([python, "-I", "-B", "-c", code, Path(__file__).resolve(), *extras])
    if errors:
        raise ValueError("Application dependency validation failed: " + "; ".join(errors))


def activate(repo, candidate):
    if candidate.parent.resolve() != repo.resolve() or not candidate.name.startswith(".venv-"):
        raise ValueError("Candidate must be a final .venv-<id> directory in the checkout")
    if not (candidate / RECORD).is_file() or not (candidate / "bin/python").is_file():
        raise ValueError("Candidate is incomplete")
    alias = repo / ".venv"
    if alias.exists() and not alias.is_symlink():
        raise ValueError("Refusing to replace a legacy .venv directory")
    temporary = repo / (".venv-link-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(candidate.name, target_is_directory=True)
        os.replace(temporary, alias)
    finally:
        temporary.unlink(missing_ok=True)


def service_state(unit):
    result = subprocess.run(["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
                            check=True, capture_output=True, text=True)
    state = result.stdout.strip()
    if state not in {"active", "inactive", "failed", ""}:
        raise ValueError(f"{unit} is {state}; wait for its current operation to finish")
    return state == "active"


def systemctl(action, unit):
    run(["systemctl", "--user", action, unit], timeout=120)


APP_PROBE = '''
import asyncio, json, sys
from urllib.request import Request, urlopen
from hfp_mcp.settings import RuntimeConfig
from hfp_mcp.routing import RoutingConfig
try:
    config, routing = RuntimeConfig.load(), RoutingConfig.load()
    mode, profile = sys.argv[1:3]
    if mode in {'config', 'bridge-config'}:
        from pathlib import Path
        import hfp_mcp
        if not Path(hfp_mcp.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError('application must be installed in this venv, not editable or inherited')
        if mode == 'bridge-config':
            endpoints = [endpoint for endpoint in routing.endpoints.values() if endpoint.profile == profile]
            if not endpoints: raise ValueError('missing configured Hermes endpoint')
            for endpoint in endpoints:
                endpoint.secret()
                endpoint.secret(True)
        result = {'configured_phone': bool(config.device_address), 'routing_enabled': routing.enabled}
    elif mode == 'idle':
        from hfp_mcp.phone_cli import control
        status = control('/v1/phone')
        call_state = control('/v1/state').get('call', {}).get('state')
        terminal = {'completed', 'failed', 'cancelled', 'interrupted', 'uncertain'}
        active = sum(task.get('status') not in terminal
                     for group in status.get('native_phone_tasks', []) for task in group.get('tasks', []))
        unavailable = any(group.get('status') != 'ok' for group in status.get('native_phone_tasks', []))
        firing = any(row.get('status') == 'firing' for row in status.get('scheduled_callbacks', []))
        controller_idle = not status.get('enabled', False) or status.get('state') == 'idle'
        result = {'idle': call_state == 'idle' and controller_idle and not active and not firing,
                  'tasks_unavailable': unavailable}
    else:
        base = (config.daemon_url or f'http://127.0.0.1:{config.port}').removesuffix('/mcp').rstrip('/')
        if mode == 'daemon':
            path = '/readyz' if config.device_address else '/healthz'
            with urlopen(Request(base + path), timeout=5) as response:
                result = json.load(response)
            if not result.get('ok'): raise ValueError('daemon not ready')
        else:
            from hfp_mcp.hermes_api import HermesAPI
            async def check():
                endpoints = [endpoint for endpoint in routing.endpoints.values() if endpoint.profile == profile]
                if not endpoints: raise ValueError('missing configured Hermes endpoint for authenticated readiness')
                for endpoint in endpoints:
                    api = HermesAPI(endpoint)
                    try: await asyncio.wait_for(api.check(), timeout=8)
                    finally: await api.close()
            asyncio.run(check())
            result = {'bridge': True}
    print(json.dumps(result))
except Exception as exc:
    print(json.dumps({'error': type(exc).__name__}))
    sys.exit(1)
'''


def app_probe(python, mode, *, home=None):
    profile = Path(home).name if home and Path(home).parent.name == "profiles" else "default"
    return json_command([python, "-I", "-B", "-c", APP_PROBE, mode, profile], timeout=35)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "activate"])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--with-gemini", action="store_true")
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        if args.action == "activate":
            if args.environment is None:
                parser.error("activate requires --environment")
            activate(repo, args.environment.absolute())
        else:
            with update_lock(repo):
                validate_fresh(repo)
                info = probe_python(args.python, native=True)
                home, launcher = validate_home(args.hermes_home, args.hermes)
                extras = ["daemon"] + (["gemini-live"] if args.with_gemini else [])
                print(build_environment(repo, info, extras, home, launcher))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Environment preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
