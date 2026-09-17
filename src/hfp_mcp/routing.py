"""Explicit, immutable caller routing. No Hermes imports or Bluetooth ownership."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .contracts import normalize_phone_number
from .settings import _load_yaml, service_environment

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Endpoint:
    profile: str
    url: str
    token_env: str
    bridge_token_env: str

    def secret(self, bridge: bool = False) -> str:
        value = service_environment().get(
            self.bridge_token_env if bridge else self.token_env, ""
        )
        if len(value) < 32:
            raise ValueError(f"missing or short credential for profile {self.profile}")
        return value


@dataclass(frozen=True)
class Policy:
    admin: bool = False
    tools: frozenset[str] = frozenset()
    remember: bool = True
    max_minutes: int = 10
    background_tasks: bool = False

    def allows(self, name: str) -> bool:
        return self.admin or name in self.tools


@dataclass(frozen=True)
class Route:
    endpoint: str
    policy: str
    voice: str = "gemini_live"
    fallback: str | None = None
    continuity: bool = False


@dataclass(frozen=True)
class RoutingConfig:
    enabled: bool = False
    region: str = "IN"
    endpoints: Mapping[str, Endpoint] = field(default_factory=dict)
    policies: Mapping[str, Policy] = field(default_factory=dict)
    numbers: Mapping[str, Route] = field(default_factory=dict)
    default: Route | None = None
    blocked: frozenset[str] = frozenset()
    auto_answer: bool = True
    auto_reconnect: bool = False
    max_phone_tasks: int = 2
    background_task_minutes: int = 30

    @classmethod
    def load(cls, path: Path | None = None) -> "RoutingConfig":
        path = (
            path
            or Path(
                service_environment().get(
                    "HFP_MCP_CONFIG", "~/.config/hfp-mcp/config.yaml"
                )
            ).expanduser()
        )
        root = _load_yaml(path)
        return cls.parse(
            root.get("phone"),
            region=service_environment().get(
                "HFP_DEFAULT_REGION",
                root.get("bluetooth", {}).get("default_region", "IN"),
            ),
        )

    @classmethod
    def parse(cls, data: dict | None, *, region: str = "IN") -> "RoutingConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("phone.version must be 1")
        from .settings import _bool

        def boolean(value, default):
            return _bool(value, default)

        region = str(data.get("default_region", region)).upper()
        endpoints = {}
        for name, value in data.get("endpoints", {}).items():
            if not _NAME.fullmatch(name):
                raise ValueError("invalid endpoint name")
            endpoint = Endpoint(**value)
            url = urlsplit(endpoint.url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(f"invalid endpoint URL: {name}")
            if url.scheme == "http" and url.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Hermes endpoints require loopback HTTP or HTTPS")
            if not _NAME.fullmatch(endpoint.profile):
                raise ValueError("invalid Hermes profile name")
            if endpoint.token_env == endpoint.bridge_token_env:
                raise ValueError("API and phone-binding credentials must be distinct")
            endpoints[name] = endpoint
        policies = {}
        for name, value in data.get("policies", {}).items():
            if not _NAME.fullmatch(name) or not isinstance(value, dict):
                raise ValueError("invalid policy")
            tools = value.get("tools", [])
            if not isinstance(tools, list) or any(
                not isinstance(t, str) or not t or "*" in t for t in tools
            ):
                raise ValueError("tools must be exact registered tool identifiers")
            minutes = int(value.get("max_minutes", 10))
            if not 1 <= minutes <= 240:
                raise ValueError("max_minutes must be between 1 and 240")
            policies[name] = Policy(
                boolean(value.get("admin"), False),
                frozenset(tools),
                boolean(value.get("remember"), True),
                minutes,
                boolean(value.get("background_tasks"), False),
            )

        def route(value):
            value = dict(value)
            value["continuity"] = boolean(value.get("continuity"), False)
            result = Route(**value)
            if result.endpoint not in endpoints or result.policy not in policies:
                raise ValueError("route references an undefined endpoint or policy")
            if result.voice not in {
                "gemini_live",
                "classic",
            } or result.fallback not in {None, "classic"}:
                raise ValueError(
                    "voice must be gemini_live or classic; fallback may be classic"
                )
            return result

        numbers = {}
        for raw, value in data.get("numbers", {}).items():
            number = normalize_phone_number(str(raw), region)
            if number in numbers:
                raise ValueError("duplicate normalized caller route")
            numbers[number] = route(value)
        blocked = frozenset(
            normalize_phone_number(str(n), region) for n in data.get("blocked", [])
        )
        if blocked.intersection(numbers):
            raise ValueError("a number cannot be both routed and blocked")
        default = route(data["default"]) if data.get("default") else None
        if default and policies[default.policy].admin:
            raise ValueError("the default route cannot grant admin access")
        max_tasks = int(data.get("max_phone_tasks", 2))
        background_minutes = int(data.get("background_task_minutes", 30))
        if not 1 <= max_tasks <= 8 or not 1 <= background_minutes <= 240:
            raise ValueError("max_phone_tasks must be 1..8 and background_task_minutes 1..240")
        if any(p.background_tasks and not p.admin for p in policies.values()):
            raise ValueError("background_tasks requires an admin policy")
        return cls(
            True,
            region,
            endpoints,
            policies,
            numbers,
            default,
            blocked,
            boolean(data.get("auto_answer"), True),
            boolean(data.get("auto_reconnect"), False),
            max_tasks, background_minutes,
        )

    def resolve(
        self, number: str | None, *, presented: bool = True
    ) -> tuple[Route | None, str]:
        if not self.enabled:
            return None, "routing_disabled"
        normalized = None
        if number and presented:
            try:
                normalized = normalize_phone_number(number, self.region)
            except ValueError:
                pass
        if normalized in self.blocked:
            return None, "blocked"
        if normalized in self.numbers:
            return self.numbers[normalized], "number_match"
        return self.default, "default" if self.default else "unmapped"

    def explain(self, number: str | None) -> dict:
        from dataclasses import asdict

        route, reason = self.resolve(number)
        return {
            "reason": reason,
            "route": asdict(route) if route else None,
            "profile": self.endpoints[route.endpoint].profile if route else None,
        }
