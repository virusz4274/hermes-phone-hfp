"""HTTP authentication and secure runtime-path helpers."""

from __future__ import annotations

import os
import secrets
import stat
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from starlette.responses import JSONResponse


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    return host.strip("[]").lower() in LOOPBACK_HOSTS


def xdg_runtime_path(filename: str) -> Path:
    root = os.environ.get("XDG_RUNTIME_DIR")
    if root:
        return Path(root) / "hfp-mcp" / filename
    return Path(f"/run/user/{os.getuid()}") / "hfp-mcp" / filename


def xdg_state_path(filename: str) -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "hfp-mcp" / filename
    return Path.home() / ".local" / "state" / "hfp-mcp" / filename


def load_or_create_token(path: Path, explicit: str | None = None) -> str:
    """Load a control bearer token, creating a mode-0600 token when absent."""
    if explicit:
        if len(explicit) < 32:
            raise ValueError("HFP MCP bearer token must contain at least 32 characters")
        return explicit
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"token file {path} must not be group/world accessible")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError(f"token file {path} contains an invalid token")
        return token
    token = secrets.token_urlsafe(48)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode())
    finally:
        os.close(descriptor)
    return token


class BearerAuthMiddleware:
    """Require a fixed bearer token for every HTTP control-plane request."""

    def __init__(
        self,
        app,
        *,
        token: str,
        public_paths: Iterable[str] = ("/healthz",),
    ) -> None:
        self.app = app
        self._token = token
        self._public = frozenset(public_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._public:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        value = headers.get(b"authorization", b"").decode("latin-1")
        supplied = value[7:] if value.lower().startswith("bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, self._token):
            response = JSONResponse(
                {"ok": False, "error": {"code": "unauthorized", "message": "valid bearer token required", "retryable": False}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class HostOriginMiddleware:
    """Apply DNS-rebinding and browser-origin policy to HTTP *and* WebSocket."""

    def __init__(self, app, *, allowed_hosts: Iterable[str], allowed_origins: Iterable[str]) -> None:
        self.app = app
        self._hosts = tuple(item.lower() for item in allowed_hosts)
        self._origins = tuple(item.rstrip("/").lower() for item in allowed_origins)

    @staticmethod
    def _matches(value: str, patterns: tuple[str, ...]) -> bool:
        normalized = value.rstrip("/").lower()
        for pattern in patterns:
            # Host/origin port wildcards are the common policy form. Handle
            # them literally so bracketed IPv6 addresses are not interpreted
            # as fnmatch character classes.
            if pattern.endswith(":*"):
                base = pattern[:-2]
                if normalized.startswith(base + ":"):
                    port = normalized[len(base) + 1 :]
                    if port.isdigit():
                        return True
                continue
            if "*" not in pattern and "?" not in pattern:
                if normalized == pattern:
                    return True
                continue
            if fnmatchcase(normalized, pattern):
                return True
        return False

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {
            key.lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        host = headers.get(b"host", "")
        origin = headers.get(b"origin", "")
        rejected = not host or not self._matches(host, self._hosts)
        if origin and not self._matches(origin, self._origins):
            rejected = True
        if not rejected:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "untrusted host or origin",
                }
            )
            return
        response = JSONResponse(
            {
                "ok": False,
                "error": {
                    "code": "untrusted_host",
                    "message": "request host or origin is not allowed",
                    "retryable": False,
                },
            },
            status_code=421,
        )
        await response(scope, receive, send)
