#!/usr/bin/env python3
"""Update a fresh-layout installation without changing host packages or Hermes Python."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup import runtime
from setup.install_hermes import managed_script_command


def hermes_env(record):
    return {**os.environ, "HERMES_HOME": record["hermes_home"]}


def hermes_running(record):
    """Ask native process-identity checks; do not guess from PID/argv substrings."""
    command = managed_script_command(
        record["hermes_launcher"], Path(__file__).resolve(), ["--managed-probe"],
        env=hermes_env(record),
    )
    return runtime.json_command(command, env=hermes_env(record))["running"]


def preflight(repo, requested_python=None, *, check_runtime=False):
    previous, record = runtime.read_installation(repo)
    if check_runtime and not (previous / "bin/python").is_file():
        raise ValueError("Broken daemon venv: Python is missing; follow the clean-reinstall instructions")
    info = runtime.probe_python(requested_python or record["base_python"], native=True)
    old_minor = record["python_version"].split(".")[:2]
    if not requested_python and info["python_version"].split(".")[:2] != old_minor:
        raise ValueError("Host Python minor version changed; explicitly select a supported interpreter with --python")
    if info["python_version"] != record["python_version"]:
        print(f"Python drift: {record['python_version']} -> {info['python_version']}")
    home, launcher = runtime.validate_home(record["hermes_home"], record["hermes_launcher"])
    record = {**record, "hermes_home": home, "hermes_launcher": launcher}
    # Configuration parsing runs in a subprocess. The updater itself has no
    # application, native-binding or third-party imports.
    if check_runtime:
        runtime.check_dependencies(previous / "bin/python", record["extras"])
        runtime.app_probe(previous / "bin/python", "config", home=home)
        if home:
            runtime.app_probe(previous / "bin/python", "bridge-config", home=home)
    return previous, record, info


def require_idle(python, *, home=None, check_tasks=True):
    status = runtime.app_probe(python, "idle", home=home)
    if not status["idle"] or (check_tasks and status["tasks_unavailable"]):
        raise ValueError("Calls or phone workers are active/unavailable; finish them before updating")


def wait_ready(python, record, *, daemon, gateway, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if daemon:
                if not runtime.service_state(runtime.DAEMON_UNIT):
                    raise ValueError("Daemon service did not start")
                runtime.app_probe(python, "daemon", home=record["hermes_home"])
            if gateway:
                if not runtime.service_state(runtime.GATEWAY_UNIT):
                    raise ValueError("Hermes gateway did not start")
                runtime.app_probe(python, "bridge", home=record["hermes_home"])
            return
        except (ValueError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise ValueError("Post-update readiness failed; inspect daemon/gateway logs") from None
            time.sleep(2)


def perform_update(repo, *, python=None, check=False):
    previous, record, info = preflight(repo, python, check_runtime=check)
    if check:
        # No lock creation, Hermes bootstrap, pip/build calls, or service mutations.
        print(f"Ready to prepare a new {runtime.PYTHON_RANGE} daemon venv with {info['base_python']}")
        print(f"Extras: {', '.join(record['extras'])}; Hermes home: {record['hermes_home'] or 'none'}")
        return
    with runtime.update_lock(repo):
        # Re-read under the lock in case another updater changed the selection.
        previous, record, info = preflight(repo, python)
        daemon_active = runtime.service_state(runtime.DAEMON_UNIT)
        gateway_active = bool(record["hermes_home"]) and runtime.service_state(runtime.GATEWAY_UNIT)
        if record["hermes_home"]:
            running = hermes_running(record)
            if running and not gateway_active:
                raise ValueError("A foreground/custom gateway is running; stop it manually before updating")
            runtime.run([
                info["base_python"], "-B", repo / "scripts/check_hermes.py",
                "--hermes", record["hermes_launcher"],
            ], env=hermes_env(record), timeout=150)
        if daemon_active:
            require_idle(previous / "bin/python", home=record["hermes_home"], check_tasks=gateway_active)
        print("Preparing replacement environment; current services remain selected.", flush=True)
        candidate = runtime.build_environment(
            repo, info, record["extras"], record["hermes_home"], record["hermes_launcher"],
        )
        runtime.app_probe(candidate / "bin/python", "config", home=record["hermes_home"])
        if gateway_active:
            runtime.app_probe(candidate / "bin/python", "bridge-config", home=record["hermes_home"])
        gateway_stopped = False
        cutover = False
        try:
            if daemon_active:
                require_idle(previous / "bin/python", home=record["hermes_home"], check_tasks=gateway_active)
            if gateway_active:
                # No --yes/--interrupt: native Hermes refuses active work.
                runtime.run([record["hermes_launcher"], "gateway", "stop"], env=hermes_env(record), timeout=120)
                gateway_stopped = True
                if hermes_running(record) or runtime.service_state(runtime.GATEWAY_UNIT):
                    raise ValueError("Hermes gateway is still running; stop it before updating")
            if daemon_active:
                require_idle(previous / "bin/python", home=record["hermes_home"], check_tasks=False)
            cutover = True
            if daemon_active:
                runtime.systemctl("stop", runtime.DAEMON_UNIT)
            if record["hermes_home"]:
                runtime.run([
                    info["base_python"], "-B", repo / "setup/install_hermes.py",
                    "--home", record["hermes_home"], "--hermes", record["hermes_launcher"],
                ], env=hermes_env(record), timeout=1800)
            runtime.activate(repo, candidate)
            if gateway_active:
                runtime.run([record["hermes_launcher"], "gateway", "start"], env=hermes_env(record), timeout=120)
            if daemon_active:
                runtime.systemctl("start", runtime.DAEMON_UNIT)
            wait_ready(candidate / "bin/python", record, daemon=daemon_active, gateway=gateway_active)
        except BaseException:
            if cutover:
                # PM owns its own publication recovery. We do not attempt to roll
                # back a gateway/plugin/daemon/database combination automatically.
                for unit, affected in ((runtime.DAEMON_UNIT, daemon_active),
                                       (runtime.GATEWAY_UNIT, gateway_active)):
                    if affected:
                        try:
                            result = subprocess.run(["systemctl", "--user", "stop", unit], check=False, timeout=120)
                            if result.returncode:
                                print(f"Could not stop {unit}; stop it manually before recovery.", file=sys.stderr)
                        except (OSError, subprocess.SubprocessError):
                            print(f"Could not stop {unit}; stop it manually before recovery.", file=sys.stderr)
                print(f"Cutover failed; affected services are stopped. Previous environment: {previous}. "
                      f"Candidate: {candidate}. See docs/maintenance.md for recovery.", file=sys.stderr)
            elif gateway_stopped:
                try:
                    runtime.run([record["hermes_launcher"], "gateway", "start"], env=hermes_env(record), timeout=120)
                except (OSError, subprocess.SubprocessError):
                    print("Could not restart the unchanged Hermes gateway; start it manually.", file=sys.stderr)
            raise
        print(f"Update complete: {candidate.name}. Previous environment retained: {previous.name}.")
        if not daemon_active or (record["hermes_home"] and not gateway_active):
            print("Previously stopped services remain stopped; live checks cover only restarted services.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help="Explicitly choose an already installed host interpreter")
    parser.add_argument("--check", action="store_true", help="Read-only layout, native-binding and configuration checks")
    parser.add_argument("--managed-probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.managed_probe:
        from gateway.status import live_gateway_pid_for_home
        home = Path(os.environ["HERMES_HOME"])
        print(json.dumps({"running": live_gateway_pid_for_home(home) is not None}))
        return
    if os.geteuid() == 0:
        parser.error("Run updates as the service user, without sudo")
    try:
        perform_update(Path(__file__).resolve().parents[1], python=args.python, check=args.check)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Update failed: {exc}\n")


if __name__ == "__main__":
    main()
