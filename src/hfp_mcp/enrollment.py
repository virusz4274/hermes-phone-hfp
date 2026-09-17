"""Time-bounded, first-device Bluetooth enrollment gate.

The canonical daemon may start before a phone is configured, but its pairing
agent and HFP profile remain fail-closed unless this private runtime gate is
open.  The first device that requests pairing owns the window; all others are
rejected until the gate closes.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

from .contracts import validate_mac
from .security import xdg_runtime_path


def gate_path() -> Path:
    return xdg_runtime_path("enrollment.json")


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def open_gate(timeout_seconds: int) -> Path:
    timeout = int(timeout_seconds)
    if timeout < 30 or timeout > 600:
        raise ValueError("enrollment timeout must be between 30 and 600 seconds")
    path = gate_path()
    _atomic_private_write(
        path,
        json.dumps(
            {"expires_at": time.time() + timeout, "candidate_address": None},
            separators=(",", ":"),
        ),
    )
    return path


def close_gate() -> None:
    try:
        gate_path().unlink()
    except FileNotFoundError:
        pass


def _read_gate() -> tuple[Path, dict] | None:
    path = gate_path()
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or float(payload.get("expires_at", 0)) <= time.time():
            close_gate()
            return None
        return path, payload
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def enrollment_candidate() -> str | None:
    gate = _read_gate()
    if gate is None:
        return None
    candidate = gate[1].get("candidate_address")
    try:
        return validate_mac(str(candidate)) if candidate else None
    except ValueError:
        return None


def authorize_enrollment_address(address: str, *, claim: bool = True) -> bool:
    """Allow only the first Bluetooth address observed during an open window."""
    normalized = validate_mac(address)
    gate = _read_gate()
    if gate is None:
        return False
    path, payload = gate
    candidate = payload.get("candidate_address")
    if candidate:
        try:
            return validate_mac(str(candidate)) == normalized
        except ValueError:
            return False
    if not claim:
        return True
    payload["candidate_address"] = normalized
    _atomic_private_write(path, json.dumps(payload, separators=(",", ":")))
    return True


def address_from_bluez_path(device_path: str) -> str:
    marker = "/dev_"
    if marker not in device_path:
        raise ValueError("invalid BlueZ device path")
    return validate_mac(device_path.rsplit(marker, 1)[1].replace("_", ":"))


def set_phone_in_service_env(path: Path, address: str) -> None:
    """Atomically set one exact phone while preserving all operator settings."""
    normalized = validate_mac(address)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    keys = {"HFP_PHONE_ADDRESS", "HFP_PHONE_DEFAULT_ADDRESS"}
    replaced: set[str] = set()
    for line in lines:
        stripped = line.strip()
        matched = next((key for key in keys if stripped.startswith(f"{key}=")), None)
        if matched is not None:
            if matched not in replaced:
                output.append(f"{matched}={normalized}")
                replaced.add(matched)
            continue
        output.append(line)
    missing = [key for key in sorted(keys) if key not in replaced]
    if missing:
        output.append("")
        output.extend(f"{key}={normalized}" for key in missing)
    _atomic_private_write(path, "\n".join(output).rstrip() + "\n")
