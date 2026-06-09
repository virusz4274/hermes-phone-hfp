#!/usr/bin/env python3
"""Render the default hfp-mcp user service environment."""

from __future__ import annotations

import argparse
import re
import subprocess


_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", default="", help="Host/IP to place in returned audio stream URLs")
    args = parser.parse_args()
    public_host = args.public_host.strip() or detect_public_host()
    print(render_env(public_host), end="")


if __name__ == "__main__":
    main()
