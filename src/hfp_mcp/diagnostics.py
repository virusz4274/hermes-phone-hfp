"""Deployment diagnostics and time-bounded Bluetooth enrollment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .config import HFP_AG_UUID
from .enrollment import (
    close_gate,
    enrollment_candidate,
    open_gate,
    set_phone_in_service_env,
)
from .settings import RuntimeConfig


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _command_ok(*command: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail


def _permission_check(path: Path, *, required: bool) -> Check:
    if not path.exists():
        return Check(path.name, "error" if required else "warning", f"missing: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return Check(path.name, "error", f"{path} mode is {mode:o}; expected 600")
    return Check(path.name, "ok", f"{path} mode {mode:o}")


def _daemon_health_url(config: RuntimeConfig) -> str:
    if config.daemon_url:
        parsed = urlsplit(config.daemon_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/healthz", "", ""))
    scheme = "https" if config.tls_cert else "http"
    host = config.host
    if host in {"0.0.0.0", "::"}:
        host = config.public_host or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{config.port}/healthz"


def _fetch_daemon_health(config: RuntimeConfig) -> tuple[bool, str]:
    url = _daemon_health_url(config)
    try:
        with urlopen(
            Request(url, headers={"Accept": "application/json"}),
            timeout=3,
        ) as response:
            payload = json.loads(response.read(64 * 1024))
    except (OSError, ValueError, HTTPError, URLError, json.JSONDecodeError) as exc:
        return False, f"{url}: {exc}"
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False, f"{url}: invalid health response"
    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    return True, f"{url}: bluez={health.get('bluez', 'unknown')}"


def run_doctor(
    config: RuntimeConfig, *, hermes_home: Path | None = None, offline: bool = False
) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("config", "ok", "typed configuration is valid"))
    checks.append(
        Check(
            "configured-phone",
            "ok" if config.device_address else "error",
            config.device_address or "HFP_PHONE_ADDRESS is not set",
        )
    )
    checks.append(
        Check(
            "self-call-guard",
            "ok" if config.self_number else "warning",
            (
                "paired-handset cellular number is configured"
                if config.self_number
                else "HFP_PHONE_SELF_NUMBER is not set; the daemon cannot identify self-calls"
            ),
        )
    )
    if config.admin_approval_mode == "bypass":
        checks.append(
            Check(
                "admin-approval",
                "warning",
                "bypass is enabled; caller-ID spoofing can unlock privileged Hermes tools",
            )
        )
    checks.append(
        Check(
            "network",
            "ok",
            f"control bind {config.host}:{config.port}; TLS={'direct' if config.tls_cert else 'proxy' if config.trusted_tls_proxy else 'loopback'}",
        )
    )
    checks.append(_permission_check(config.bearer_token_file, required=False))
    env_file = Path(os.getenv("HFP_MCP_ENV_FILE", "~/.config/hfp-mcp.env")).expanduser()
    if env_file.exists():
        checks.append(_permission_check(env_file, required=True))

    for executable in ("bluetoothctl", "ffmpeg"):
        path = shutil.which(executable)
        checks.append(
            Check(executable, "ok" if path else "error", path or "not installed")
        )

    bluez_ok, bluez_detail = _command_ok("systemctl", "is-active", "bluetooth.service")
    checks.append(
        Check("bluez", "ok" if bluez_ok else "error", bluez_detail or "inactive")
    )
    service_ok, service_detail = _command_ok(
        "systemctl", "--user", "is-active", "hfp-mcp.service"
    )
    checks.append(
        Check(
            "hfp-service",
            "ok" if service_ok else "error",
            service_detail or "inactive",
        )
    )
    if offline:
        checks.append(
            Check("daemon-health", "warning", "HTTP check skipped (--offline)")
        )
    else:
        health_ok, health_detail = _fetch_daemon_health(config)
        checks.append(
            Check("daemon-health", "ok" if health_ok else "error", health_detail)
        )
    if shutil.which("bluetoothctl"):
        ok, detail = _command_ok("bluetoothctl", "show")
        checks.append(Check("adapter", "ok" if ok else "error", detail or "no adapter"))
        if config.device_address:
            ok, detail = _command_ok("bluetoothctl", "info", config.device_address)
            paired = ok and "Paired: yes" in detail
            trusted = ok and "Trusted: yes" in detail
            hfp_ag = ok and HFP_AG_UUID.lower() in detail.lower()
            checks.append(
                Check(
                    "phone",
                    "ok" if paired and trusted and hfp_ag else "error",
                    (
                        f"{config.device_address}: paired={paired}, "
                        f"trusted={trusted}, hfp_ag={hfp_ag}"
                    ),
                )
            )

    wireplumber = shutil.which("wpctl") or shutil.which("wireplumber")
    wireplumber_overrides = (
        Path("/etc/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf"),
        Path("/etc/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua"),
        Path.home() / ".config/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf",
        Path.home() / ".config/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua",
    )
    wireplumber_override = next(
        (path for path in wireplumber_overrides if path.exists()), None
    )
    checks.append(
        Check(
            "wireplumber",
            "ok" if not wireplumber or wireplumber_override is not None else "warning",
            str(wireplumber_override)
            if wireplumber_override is not None
            else "installed without hfp-mcp role override"
            if wireplumber
            else "not detected",
        )
    )

    home = Path(hermes_home or os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    plugin = home / "plugins" / "hfp-phone"
    plugin_yaml = plugin / "plugin.yaml"
    checks.append(
        Check(
            "hermes-plugin",
            "ok" if plugin_yaml.exists() else "warning",
            str(plugin_yaml)
            if plugin_yaml.exists()
            else "hfp-phone is not installed for this user",
        )
    )
    from .preflight import routing_checks

    checks.extend(asyncio.run(routing_checks(offline=offline)))
    return checks


def doctor_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the HFP MCP deployment")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--hermes-home",
        type=Path,
        help="Local Hermes profile home (default: HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Check local prerequisites without HTTP requests",
    )
    args = parser.parse_args(argv)
    try:
        config = RuntimeConfig.load()
        checks = run_doctor(config, hermes_home=args.hermes_home, offline=args.offline)
    except Exception as exc:
        checks = [
            Check(
                "config",
                "error",
                f"Configuration could not be loaded ({type(exc).__name__}); check YAML and private environment permissions",
            )
        ]
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            print(f"{check.status.upper():7} {check.name:18} {check.detail}")
    return 1 if any(check.status == "error" for check in checks) else 0


def wait_live_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wait for the hfp-mcp user service and HTTP liveness endpoint"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.timeout > 300:
        parser.error("--timeout must be greater than 0 and at most 300 seconds")
    try:
        config = RuntimeConfig.load()
    except Exception as exc:
        print(f"invalid hfp-mcp configuration: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + args.timeout
    last_detail = "service has not started"
    while True:
        service_ok, service_detail = _command_ok(
            "systemctl", "--user", "is-active", "hfp-mcp.service"
        )
        health_ok, health_detail = _fetch_daemon_health(config)
        last_detail = health_detail or service_detail or last_detail
        if service_ok and health_ok:
            print(f"hfp-mcp is live: {health_detail}")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"hfp-mcp did not become live within {args.timeout:g}s: {last_detail}",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(0.5, remaining))


def _paired_hfp_addresses() -> list[str]:
    ok, devices = _command_ok("bluetoothctl", "devices", "Paired")
    if not ok:
        return []
    addresses: list[str] = []
    for line in devices.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        address = parts[1].upper()
        info_ok, info = _command_ok("bluetoothctl", "info", address)
        if info_ok and "Paired: yes" in info and HFP_AG_UUID.lower() in info.lower():
            addresses.append(address)
    return sorted(set(addresses))


def _find_enrolled_hfp_phone() -> str | None:
    candidate = enrollment_candidate()
    addresses = _paired_hfp_addresses()
    if candidate and candidate in addresses:
        return candidate
    # A pre-existing sole pairing must not instantly consume a replacement
    # enrollment window. The restricted daemon agent records the address that
    # actually claimed this gate; installer adoption is the separate path for
    # intentionally selecting an already-paired sole phone.
    return None


def enroll_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Temporarily enable Bluetooth enrollment"
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.timeout < 30 or args.timeout > 600:
        parser.error("--timeout must be between 30 and 600 seconds")
    if not shutil.which("bluetoothctl"):
        print("bluetoothctl is not installed", file=sys.stderr)
        return 1
    service_ok, _ = _command_ok("systemctl", "--user", "is-active", "hfp-mcp.service")
    if not service_ok:
        print(
            "hfp-mcp.service must be running so its restricted pairing agent owns the HFP profile",
            file=sys.stderr,
        )
        return 1
    commands = (
        ("power", "on"),
        ("pairable", "on"),
        ("discoverable", "on"),
    )
    selected_phone: str | None = None
    try:
        open_gate(args.timeout)
        for command in commands:
            ok, detail = _command_ok("bluetoothctl", *command)
            if not ok:
                raise RuntimeError(detail or f"bluetoothctl {' '.join(command)} failed")
        print(
            f"Bluetooth enrollment enabled for {args.timeout} seconds. "
            "The running hfp-mcp daemon owns the pairing agent; confirm the code on both devices."
        )
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            selected_phone = _find_enrolled_hfp_phone()
            if selected_phone:
                break
            time.sleep(min(1.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        close_gate()
        _command_ok("bluetoothctl", "scan", "off")
        _command_ok("bluetoothctl", "discoverable", "off")
        _command_ok("bluetoothctl", "pairable", "off")
    print("Bluetooth enrollment closed; the adapter is no longer pairable.")
    if not selected_phone:
        print(
            "No unique paired HFP phone was enrolled before the timeout.",
            file=sys.stderr,
        )
        return 1
    trust_ok, trust_detail = _command_ok("bluetoothctl", "trust", selected_phone)
    if not trust_ok:
        print(trust_detail or f"could not trust {selected_phone}", file=sys.stderr)
        return 1
    env_path = Path(
        os.environ.get("HFP_MCP_ENV_FILE", "~/.config/hfp-mcp.env")
    ).expanduser()
    set_phone_in_service_env(env_path, selected_phone)
    restart_ok, restart_detail = _command_ok(
        "systemctl", "--user", "restart", "hfp-mcp.service"
    )
    if not restart_ok:
        print(restart_detail or "could not restart hfp-mcp.service", file=sys.stderr)
        return 1
    print(f"Enrolled, trusted, and configured HFP phone {selected_phone}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"route", "caller", "phone", "transcript"}:
        from .phone_cli import main as phone_main

        return phone_main(argv)
    parser = argparse.ArgumentParser(prog="hfp-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "route": "Validate or explain caller routing",
        "caller": "Inspect or forget caller notes",
        "phone": "Check phone status or respond to an approval",
        "transcript": "List or export retained call transcripts",
    }.items():
        subparsers.add_parser(name, help=help_text)
    subparsers.add_parser("doctor")
    wait_live = subparsers.add_parser("wait-live")
    wait_live.add_argument("--timeout", type=float, default=30.0)
    enroll = subparsers.add_parser("enroll")
    enroll.add_argument("--timeout", type=int, default=180)
    args, remaining = parser.parse_known_args(argv)
    if args.command == "doctor":
        return doctor_main(remaining)
    if args.command == "wait-live":
        return wait_live_main(["--timeout", str(args.timeout), *remaining])
    return enroll_main(["--timeout", str(args.timeout), *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
