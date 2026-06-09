#!/usr/bin/env python3
"""Render the default hfp-mcp user service environment."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from pathlib import Path


_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_DEFAULT_FLAG_VALUES = (
    ("--port", "8000"),
    ("--status-port", "8001"),
    ("--audio-host", "0.0.0.0"),
)


def choose_public_host(hostname_i: str = "", hostname_f: str = "") -> str:
    """Pick a host remote LAN clients can put in audio WebSocket URLs."""
    for part in hostname_i.split():
        if _IPV4_RE.match(part) and not part.startswith("127."):
            return part
    hostname = hostname_f.strip()
    if hostname and hostname != "localhost":
        return hostname
    return "raspberrypi.local"


def _run_hostname(*args: str) -> str:
    try:
        return subprocess.check_output(["hostname", *args], text=True).strip()
    except Exception:
        return ""


def detect_public_host() -> str:
    return choose_public_host(_run_hostname("-I"), _run_hostname("-f"))


def render_env(public_host: str) -> str:
    return f"""# Extra args for the HFP MCP service. After editing: systemctl --user restart hfp-mcp
# Default installer mode exposes MCP, status, and realtime call audio to trusted LAN clients.
HFP_MCP_OPTS="--port 8000 --status-port 8001 --audio-host 0.0.0.0 --audio-public-host {public_host}"

# Restrict who may connect to the MCP HTTP endpoint (repeatable). If you enable
# this, include every hostname/IP remote MCP clients use. The audio WebSocket is
# protected by per-session tokens returned from the MCP tool result.
#HFP_MCP_OPTS="--port 8000 --status-port 8001 --audio-host 0.0.0.0 --audio-public-host {public_host} --allowed-host {public_host}:8000"
"""


def _has_flag(tokens: list[str], flag: str) -> bool:
    return any(token == flag or token.startswith(f"{flag}=") for token in tokens)


def _quote_env_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def merge_default_opts(opts: str, public_host: str) -> str:
    """Add newer installer defaults without changing any explicitly set flags."""
    tokens = shlex.split(opts)
    for flag, value in _DEFAULT_FLAG_VALUES:
        if not _has_flag(tokens, flag):
            tokens.extend([flag, value])
    if not _has_flag(tokens, "--audio-public-host"):
        tokens.extend(["--audio-public-host", public_host])
    return shlex.join(tokens)


def migrate_env_content(content: str, public_host: str) -> tuple[str, bool]:
    """Return env-file content with missing HFP_MCP_OPTS defaults added."""
    lines = content.splitlines()
    found = False
    changed = False
    migrated: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        prefix = line[: len(line) - len(stripped)]
        if stripped.startswith("HFP_MCP_OPTS="):
            found = True
            _, _, raw_value = stripped.partition("=")
            parsed = shlex.split(raw_value, comments=True)
            opts = parsed[0] if len(parsed) == 1 else " ".join(parsed)
            new_opts = merge_default_opts(opts, public_host)
            new_line = f"{prefix}HFP_MCP_OPTS={_quote_env_value(new_opts)}"
            if new_line != line:
                changed = True
            migrated.append(new_line)
        else:
            migrated.append(line)

    if not found:
        changed = True
        if migrated and migrated[-1].strip():
            migrated.append("")
        migrated.append(
            f"HFP_MCP_OPTS={_quote_env_value(merge_default_opts('', public_host))}"
        )

    return "\n".join(migrated) + "\n", changed


def migrate_env_file(path: Path, public_host: str) -> bool:
    content = path.read_text() if path.exists() else ""
    migrated, changed = migrate_env_content(content, public_host)
    if changed:
        path.write_text(migrated)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", default="", help="Host/IP to place in returned audio stream URLs")
    parser.add_argument("--migrate-env", default="", help="Update an existing hfp-mcp env file in place")
    args = parser.parse_args()
    public_host = args.public_host.strip() or detect_public_host()
    if args.migrate_env:
        migrate_env_file(Path(args.migrate_env), public_host)
    else:
        print(render_env(public_host), end="")


if __name__ == "__main__":
    main()
