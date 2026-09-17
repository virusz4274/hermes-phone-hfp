"""Typed runtime configuration shared by the daemon and diagnostics CLI."""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .config import AT_DIAL_TIMEOUT_SECONDS
from .contracts import normalize_phone_number, validate_mac
from .security import is_loopback_host, xdg_runtime_path, xdg_state_path


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _csv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _bracket_ipv6_host(value: str) -> str:
    host = str(value).strip()
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load HFP_MCP_CONFIG") from exc
    class UniqueLoader(yaml.SafeLoader):
        pass

    def unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"duplicate configuration key at line {key_node.start_mark.line + 1}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"configuration root in {path} must be a mapping")
    return payload


def _load_service_environment(path: Path) -> dict[str, str]:
    """Read the small systemd-style environment file without shell execution."""
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"service environment {path} must have mode 0600")
    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, raw = stripped.partition("=")
        if not separator or not key or not key.replace("_", "a").isalnum():
            raise ValueError(f"invalid environment assignment at {path}:{line_number}")
        parsed = shlex.split(raw, comments=True)
        if len(parsed) > 1:
            raise ValueError(f"invalid environment value at {path}:{line_number}")
        values[key] = parsed[0] if parsed else ""
    return values


def service_environment() -> dict[str, str]:
    """Shared CLI/daemon lookup; process variables take precedence."""
    path = Path(os.getenv("HFP_MCP_ENV_FILE", "~/.config/hfp-mcp.env")).expanduser()
    return {**_load_service_environment(path), **os.environ}


@dataclass(frozen=True)
class RuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    daemon_url: str | None = None
    public_host: str | None = None
    public_base_url: str | None = None
    bearer_token: str | None = None
    bearer_token_file: Path = field(default_factory=lambda: xdg_state_path("control.token"))
    tls_cert: Path | None = None
    tls_key: Path | None = None
    trusted_tls_proxy: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    adapter_address: str | None = None
    device_address: str | None = None
    default_region: str = "IN"
    self_number: str | None = None
    owner_number: str | None = None
    admin_callers: tuple[str, ...] = ()
    trusted_callers: tuple[str, ...] = ()
    blocked_callers: tuple[str, ...] = ()
    at_dial_timeout_seconds: float = AT_DIAL_TIMEOUT_SECONDS
    pairing_timeout_seconds: int = 180
    state_file: Path = field(default_factory=lambda: xdg_runtime_path("state.json"))
    database_file: Path = field(default_factory=lambda: xdg_state_path("calls.db"))
    retention_days: int = 30
    playback_roots: tuple[Path, ...] = ()
    full_transcripts: bool = False
    admin_approval_mode: str = "normal"

    @classmethod
    def load(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        include_service_env: bool | None = None,
    ) -> "RuntimeConfig":
        process_env = dict(os.environ if environ is None else environ)
        should_load_service_env = (
            environ is None if include_service_env is None else include_service_env
        )
        if should_load_service_env:
            env_path = Path(
                process_env.get("HFP_MCP_ENV_FILE", "~/.config/hfp-mcp.env")
            ).expanduser()
            env = {**_load_service_environment(env_path), **process_env}
        else:
            # Explicit mappings are hermetic for tests and embedded callers.
            env = process_env
        config_path = Path(env.get("HFP_MCP_CONFIG", "~/.config/hfp-mcp/config.yaml")).expanduser()
        data = _load_yaml(config_path)
        server = data.get("server", {}) if isinstance(data.get("server", {}), dict) else {}
        bluetooth = data.get("bluetooth", {}) if isinstance(data.get("bluetooth", {}), dict) else {}
        privacy = data.get("privacy", {}) if isinstance(data.get("privacy", {}), dict) else {}
        approval_mode = env.get("HFP_ADMIN_APPROVAL_MODE", data.get("admin_approval_mode"))
        if approval_mode is None:
            approval_mode = (
                "bypass"
                if _bool(env.get("HFP_PHONE_ADMIN_APPROVAL_BYPASS"), False)
                else "normal"
            )

        values: dict[str, Any] = {
            "host": env.get("HFP_MCP_HOST", server.get("host", "127.0.0.1")),
            "port": int(env.get("HFP_MCP_PORT", server.get("port", 8000))),
            "daemon_url": env.get(
                "HFP_MCP_DAEMON_URL",
                env.get("HFP_PHONE_MCP_URL", server.get("daemon_url")),
            ),
            "public_host": env.get("HFP_MCP_PUBLIC_HOST", server.get("public_host")),
            "public_base_url": env.get(
                "HFP_MCP_PUBLIC_BASE_URL", server.get("public_base_url")
            ),
            "bearer_token": env.get("HFP_MCP_BEARER_TOKEN", server.get("bearer_token")),
            "bearer_token_file": Path(env.get("HFP_MCP_TOKEN_FILE", server.get("token_file", xdg_state_path("control.token")))).expanduser(),
            "tls_cert": env.get("HFP_MCP_TLS_CERT", server.get("tls_cert")),
            "tls_key": env.get("HFP_MCP_TLS_KEY", server.get("tls_key")),
            "trusted_tls_proxy": _bool(env.get("HFP_MCP_TRUSTED_TLS_PROXY", server.get("trusted_tls_proxy"))),
            "allowed_hosts": _csv(env.get("HFP_MCP_ALLOWED_HOSTS", server.get("allowed_hosts"))),
            "allowed_origins": _csv(env.get("HFP_MCP_ALLOWED_ORIGINS", server.get("allowed_origins"))),
            "adapter_address": env.get("HFP_ADAPTER_ADDRESS", bluetooth.get("adapter_address")),
            "device_address": env.get("HFP_PHONE_ADDRESS", bluetooth.get("device_address")),
            "default_region": (data.get("phone") or {}).get("default_region", env.get("HFP_DEFAULT_REGION", bluetooth.get("default_region", "IN"))),
            "self_number": env.get(
                "HFP_PHONE_SELF_NUMBER", bluetooth.get("self_number")
            ),
            "owner_number": env.get(
                "HFP_PHONE_OWNER_NUMBER", bluetooth.get("owner_number")
            ),
            "admin_callers": _csv(
                env.get("HFP_PHONE_ADMIN_CALLERS", bluetooth.get("admin_callers"))
            ),
            "trusted_callers": _csv(
                env.get("HFP_PHONE_TRUSTED_CALLERS", bluetooth.get("trusted_callers"))
            ),
            "blocked_callers": _csv(
                env.get("HFP_PHONE_BLOCKED_CALLERS", bluetooth.get("blocked_callers"))
            ),
            "at_dial_timeout_seconds": float(
                env.get(
                    "HFP_AT_DIAL_TIMEOUT_SECONDS",
                    bluetooth.get(
                        "at_dial_timeout_seconds", AT_DIAL_TIMEOUT_SECONDS
                    ),
                )
            ),
            "pairing_timeout_seconds": int(env.get("HFP_PAIRING_TIMEOUT_SECONDS", bluetooth.get("pairing_timeout_seconds", 180))),
            "state_file": Path(env.get("HFP_MCP_STATE_FILE", server.get("state_file", xdg_runtime_path("state.json")))).expanduser(),
            "database_file": Path(env.get("HFP_MCP_DATABASE", privacy.get("database_file", xdg_state_path("calls.db")))).expanduser(),
            "retention_days": int(env.get("HFP_RETENTION_DAYS", privacy.get("retention_days", 30))),
            "playback_roots": tuple(Path(item).expanduser().resolve() for item in _csv(env.get("HFP_PLAYBACK_ROOTS", server.get("playback_roots")))),
            "full_transcripts": _bool(env.get("HFP_FULL_TRANSCRIPTS", privacy.get("full_transcripts"))),
            "admin_approval_mode": approval_mode,
        }
        if values["tls_cert"]:
            values["tls_cert"] = Path(values["tls_cert"]).expanduser()
        else:
            values["tls_cert"] = None
        if values["tls_key"]:
            values["tls_key"] = Path(values["tls_key"]).expanduser()
        else:
            values["tls_key"] = None
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.adapter_address:
            validate_mac(self.adapter_address)
        if self.device_address:
            validate_mac(self.device_address)
        if self.self_number:
            normalize_phone_number(self.self_number, self.default_region)
        for number in (
            *((self.owner_number,) if self.owner_number else ()),
            *self.admin_callers,
            *self.trusted_callers,
            *self.blocked_callers,
        ):
            normalize_phone_number(number, self.default_region)
        if not 1.0 <= self.at_dial_timeout_seconds <= 60.0:
            raise ValueError("AT dial timeout must be between 1 and 60 seconds")
        if self.pairing_timeout_seconds < 30 or self.pairing_timeout_seconds > 600:
            raise ValueError("pairing timeout must be between 30 and 600 seconds")
        if self.retention_days < 1 or self.retention_days > 365:
            raise ValueError("retention_days must be between 1 and 365")
        if self.admin_approval_mode not in {"normal", "bypass"}:
            raise ValueError("admin_approval_mode must be 'normal' or 'bypass'")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("TLS certificate and key must be configured together")
        if self.trusted_tls_proxy and not is_loopback_host(self.host):
            raise ValueError(
                "trusted TLS proxy mode requires a loopback backend bind"
            )
        if not is_loopback_host(self.host) and not (self.tls_cert and self.tls_key):
            raise ValueError(
                "non-loopback binding requires a direct TLS certificate and key"
            )
        if self.public_base_url:
            parsed = urlsplit(self.public_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("public_base_url must be an HTTP(S) origin without path or credentials")
            if parsed.scheme != "https" and not is_loopback_host(parsed.hostname):
                raise ValueError("non-loopback public_base_url must use https")
        if self.daemon_url:
            parsed = urlsplit(self.daemon_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path.rstrip("/") != "/mcp"
            ):
                raise ValueError(
                    "daemon_url must be an HTTP(S) /mcp URL without credentials or query"
                )
            if parsed.scheme != "https" and not is_loopback_host(parsed.hostname):
                raise ValueError("non-loopback daemon_url must use https")
        if self.trusted_tls_proxy and (
            not self.public_base_url
            or urlsplit(self.public_base_url).scheme != "https"
        ):
            raise ValueError(
                "trusted TLS proxy mode requires HFP_MCP_PUBLIC_BASE_URL=https://..."
            )
        for path in (self.tls_cert, self.tls_key):
            if path is not None and not path.is_file():
                raise ValueError(f"TLS file does not exist: {path}")

    def resolved_allowed_hosts(self) -> tuple[str, ...]:
        if self.allowed_hosts:
            return self.allowed_hosts
        bind_host = _bracket_ipv6_host(self.host)
        hosts = [
            bind_host,
            f"{bind_host}:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
        if self.public_host:
            public_host = _bracket_ipv6_host(self.public_host)
            hosts.append(public_host)
            hosts.append(f"{public_host}:*")
        if self.public_base_url:
            parsed = urlsplit(self.public_base_url)
            hosts.append(parsed.netloc)
        return tuple(dict.fromkeys(hosts))

    def validate_daemon_identity(self) -> None:
        if not self.device_address:
            raise ValueError(
                "HFP_PHONE_ADDRESS is required for daemon mode; enroll and configure exactly one trusted phone"
            )

    def resolved_allowed_origins(self) -> tuple[str, ...]:
        if self.allowed_origins:
            return self.allowed_origins
        schemes = ("https",) if not is_loopback_host(self.host) else ("http", "https")
        origins: list[str] = []
        if self.public_base_url:
            origins.append(self.public_base_url.rstrip("/"))
        for scheme in schemes:
            for host in (self.host, self.public_host):
                if host:
                    rendered_host = _bracket_ipv6_host(host)
                    origins.append(f"{scheme}://{rendered_host}")
                    origins.append(f"{scheme}://{rendered_host}:*")
        return tuple(dict.fromkeys(origins))
