#!/usr/bin/env python3
"""Render and securely migrate the hfp-mcp user-service environment."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import subprocess
import tempfile
from pathlib import Path


_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def choose_public_host(hostname_i: str = "", hostname_f: str = "") -> str:
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


def render_env(public_host: str, token: str | None = None) -> str:
    token = token or secrets.token_urlsafe(48)
    template = Path(__file__).with_name("hfp-mcp.env.example").read_text()
    return "# hfp-mcp migration-version=2\n" + (
        template.replace("replace-with-at-least-32-random-characters", token)
        .replace("__PUBLIC_HOST__", public_host)
    )



def _parse_assignment(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            raw = stripped[len(prefix):]
            parsed = shlex.split(raw, comments=True)
            return parsed[0] if parsed else ""
    return None


def _replace_or_append(lines: list[str], key: str, value: str) -> bool:
    prefix = f"{key}="
    rendered = f"{key}={value}"
    matches = [index for index, line in enumerate(lines) if line.strip().startswith(prefix)]
    if not matches:
        lines.append(rendered)
        return True
    changed = lines[matches[0]] != rendered or len(matches) > 1
    lines[matches[0]] = rendered
    for index in reversed(matches[1:]):
        lines.pop(index)
    return changed


def _legacy_port(lines: list[str]) -> str:
    opts = _parse_assignment(lines, "HFP_MCP_OPTS") or ""
    tokens = shlex.split(opts)
    for index, token in enumerate(tokens):
        if token == "--port" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--port="):
            return token.partition("=")[2]
    return "8000"


def migrate_env_content(
    content: str,
    public_host: str,
    token: str | None = None,
) -> tuple[str, bool]:
    """Apply the one-time v2 security migration without clobbering TLS config."""
    lines = content.splitlines()
    if any(line.strip() == "# hfp-mcp migration-version=2" for line in lines):
        # Token reconciliation is a permanent invariant, not a one-time
        # migration. The daemon token is authoritative when both exist so a
        # partial token rotation cannot strand an existing client at HTTP 401.
        canonical_token = (
            _parse_assignment(lines, "HFP_MCP_BEARER_TOKEN")
            or _parse_assignment(lines, "HFP_PHONE_MCP_TOKEN")
            or token
        )
        if not canonical_token or len(canonical_token) < 32:
            canonical_token = secrets.token_urlsafe(48)
        changed = False
        changed = _replace_or_append(
            lines, "HFP_MCP_BEARER_TOKEN", canonical_token
        ) or changed
        if _parse_assignment(lines, "HFP_PHONE_MCP_TOKEN") is not None:
            changed = _replace_or_append(
                lines, "HFP_PHONE_MCP_TOKEN", canonical_token
            ) or changed
        rendered = "\n".join(lines) + "\n"
        return rendered, changed
    changed = False
    existing_token = (
        _parse_assignment(lines, "HFP_MCP_BEARER_TOKEN")
        or _parse_assignment(lines, "HFP_PHONE_MCP_TOKEN")
        or token
    )
    control_token = (
        existing_token if existing_token and len(existing_token) >= 32 else secrets.token_urlsafe(48)
    )
    port = _parse_assignment(lines, "HFP_MCP_PORT") or _legacy_port(lines)
    old_host = (_parse_assignment(lines, "HFP_MCP_HOST") or "0.0.0.0").strip()
    old_public = (_parse_assignment(lines, "HFP_MCP_PUBLIC_HOST") or public_host).strip()
    opts = _parse_assignment(lines, "HFP_MCP_OPTS") or ""
    direct_tls = bool(
        (
            _parse_assignment(lines, "HFP_MCP_TLS_CERT")
            and _parse_assignment(lines, "HFP_MCP_TLS_KEY")
        )
        or ("--tls-cert" in opts and "--tls-key" in opts)
    )
    trusted_proxy = str(
        _parse_assignment(lines, "HFP_MCP_TRUSTED_TLS_PROXY") or ""
    ).lower() in {"1", "true", "yes", "on"}
    loopback = old_host.strip("[]").lower() in {"127.0.0.1", "::1", "localhost"}

    # A trusted proxy must not have a bypassable LAN backend. Direct TLS can
    # retain an intentional non-loopback bind; legacy plaintext cannot.
    host = old_host if direct_tls or loopback else "127.0.0.1"
    if trusted_proxy:
        host = "127.0.0.1"
    public_value = old_public if direct_tls or trusted_proxy else host
    public_base = _parse_assignment(lines, "HFP_MCP_PUBLIC_BASE_URL")
    if trusted_proxy and not public_base:
        public_base = f"https://{old_public}"

    local_url = (
        f"https://{old_public}:{port}"
        if direct_tls
        else f"http://127.0.0.1:{port}"
    )
    secure_values = {
        "HFP_MCP_HOST": host,
        "HFP_MCP_PORT": port,
        "HFP_MCP_PUBLIC_HOST": public_value,
        "HFP_MCP_BEARER_TOKEN": control_token,
        "HFP_MCP_DAEMON_URL": f"{local_url}/mcp",
    }
    # Keep already-configured compatibility clients working, but never add old
    # aliases to a fresh configuration. Existing settings are removed manually.
    if _parse_assignment(lines, "HFP_PHONE_MCP_URL") is not None:
        secure_values["HFP_PHONE_MCP_URL"] = f"{local_url}/mcp"
    if _parse_assignment(lines, "HFP_PHONE_MCP_TOKEN") is not None:
        secure_values["HFP_PHONE_MCP_TOKEN"] = control_token
    if not _parse_assignment(lines, "HFP_MCP_ALLOWED_HOSTS"):
        secure_values["HFP_MCP_ALLOWED_HOSTS"] = "127.0.0.1:*,localhost:*,[::1]:*"
    if not _parse_assignment(lines, "HFP_MCP_ALLOWED_ORIGINS"):
        secure_values["HFP_MCP_ALLOWED_ORIGINS"] = "http://127.0.0.1:*,http://localhost:*,http://[::1]:*"
    if not direct_tls:
        secure_values["HFP_MCP_OPTS"] = '""'
    if public_base:
        secure_values["HFP_MCP_PUBLIC_BASE_URL"] = public_base
    for key, value in secure_values.items():
        changed = _replace_or_append(lines, key, value) or changed
    lines.extend(
        [
            "",
            "# hfp-mcp migration-version=2",
            "# Legacy status/audio listeners were consolidated into the control port.",
        ]
    )
    changed = True
    return "\n".join(lines) + "\n", changed


def migrate_env_file(path: Path, public_host: str) -> bool:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    migrated, changed = migrate_env_content(content, public_host)
    if changed:
        _atomic_private_text(path, migrated)
    elif path.exists():
        path.chmod(0o600)
    return changed


def _atomic_private_text(path: Path, content: str) -> None:
    """Atomically replace *path* without ever exposing permissive file modes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def sync_token_file(env_path: Path, token_path: Path) -> str:
    """Copy the environment's control token into the canonical private file.

    Same-user stdio MCP proxies do not inherit the daemon's systemd
    EnvironmentFile.  Keeping this mode-0600 copy lets them authenticate to the
    one canonical daemon without spawning a second BlueZ profile owner.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines()
    token = (
        _parse_assignment(lines, "HFP_MCP_BEARER_TOKEN")
        or _parse_assignment(lines, "HFP_PHONE_MCP_TOKEN")
        or ""
    )
    if len(token) < 32:
        raise ValueError("hfp-mcp environment contains no valid control token")
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path.parent.chmod(0o700)
    _atomic_private_text(token_path, token + "\n")
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", default="")
    parser.add_argument("--migrate-env", default="")
    parser.add_argument("--sync-token-file", default="")
    args = parser.parse_args()
    public_host = args.public_host.strip() or detect_public_host()
    if args.migrate_env:
        env_path = Path(args.migrate_env)
        migrate_env_file(env_path, public_host)
        if args.sync_token_file:
            sync_token_file(env_path, Path(args.sync_token_file))
    else:
        if args.sync_token_file:
            parser.error("--sync-token-file requires --migrate-env")
        print(render_env(public_host), end="")


if __name__ == "__main__":
    main()
