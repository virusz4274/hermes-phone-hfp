"""Preflight and private backups for the host-changing Bluetooth installer."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import pwd
import shutil
import stat
import sys
import tempfile


def config_paths(home: Path) -> list[Path]:
    return [
        Path("/etc/bluetooth/main.conf"),
        Path("/etc/dbus-1/system.d/hfp-mcp.conf"),
        Path("/etc/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf"),
        Path("/etc/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua"),
        home / ".config/wireplumber/wireplumber.conf.d/90-hfp-mcp.conf",
        home / ".config/wireplumber/bluetooth.lua.d/90-hfp-mcp.lua",
        home / ".config/systemd/user/hfp-mcp.service",
        home / ".config/hfp-mcp.env",
        home / ".local/state/hfp-mcp/control.token",
    ]


def preflight(repo: Path, user: str) -> list[Path]:
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        raise ValueError("Supported preview runtime: Python 3.11–3.13")
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError("Choose a non-root SERVICE_USER")
    if not Path("/run/systemd/system").is_dir():
        raise ValueError("A running systemd host is required")
    for command in (
        "apt-get",
        "dpkg-query",
        "systemctl",
        "loginctl",
        "runuser",
        "getent",
    ):
        if not shutil.which(command):
            raise ValueError(
                f"Missing prerequisite: {command}; use Debian-family Linux"
            )
    # Whitespace would break the generated systemd ExecStart path.
    if any(c.isspace() for c in str(repo)) or any(c in str(repo) for c in "%\\"):
        raise ValueError(
            "Checkout path must not contain whitespace, percent signs, or backslashes"
        )
    required = (
        "pyproject.toml",
        "src/hfp_mcp/server.py",
        "setup/hfp-mcp.service",
        "setup/hfp-mcp.env.example",
        "setup/bluetooth-policy.conf",
        "setup/90-hfp-mcp.conf",
        "setup/90-hfp-mcp.lua",
        "setup/render_hfp_env.py",
        "setup/update_bluez_main_conf.py",
    )
    for name in required:
        if not (repo / name).is_file():
            raise ValueError(f"Incomplete checkout: missing {name}")
    paths = config_paths(Path(account.pw_dir))
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Configuration target must be a regular file: {path}")
    return paths


def backup(paths: list[Path], destination: Path) -> Path:
    """Create a unique snapshot; absence and metadata are part of the snapshot."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    snapshot = Path(tempfile.mkdtemp(prefix=stamp, dir=destination))
    entries = []
    for index, path in enumerate(paths):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Cannot back up non-regular configuration: {path}")
        entry = {"path": str(path), "existed": path.exists()}
        if path.exists():
            original = path.stat()
            saved = snapshot / str(index)
            shutil.copyfile(path, saved)
            saved.chmod(0o600)
            entry.update(
                file=saved.name,
                mode=stat.S_IMODE(original.st_mode),
                uid=original.st_uid,
                gid=original.st_gid,
                mtime_ns=original.st_mtime_ns,
            )
        entries.append(entry)
    manifest = snapshot / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "files": entries}, indent=2) + "\n")
    manifest.chmod(0o600)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    try:
        paths = preflight(args.repo.resolve(), args.user)
        if args.backup_dir:
            print(backup(paths, args.backup_dir))
        else:
            print("Host and configuration preflight passed; no changes made.")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Preflight failed: {exc}\n")


if __name__ == "__main__":
    main()
