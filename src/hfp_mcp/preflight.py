"""Read-only routing and provider prerequisite checks used by doctor."""

from __future__ import annotations

import asyncio

import httpx

from .hermes_api import HermesAPI
from .routing import RoutingConfig
from .settings import service_environment


async def endpoint_checks(name, endpoint, routes):
    from .diagnostics import Check

    key = f"hermes:{name}"
    api = None
    try:
        endpoint.secret()
        endpoint.secret(True)
        api = HermesAPI(endpoint)
        capabilities = await asyncio.wait_for(api.check(), timeout=10)
        checks = [
            Check(key, "ok", "API and bridge authenticated; profile and Runs API match")
        ]
        if any(route.continuity for route in routes):
            ready = capabilities.get("conversation_sessions") and capabilities.get(
                "parallel_phone_tasks"
            )
            checks.append(
                Check(
                    key + ":continuity",
                    "ok" if ready else "error",
                    "Native sessions/tasks available"
                    if ready
                    else "Continuity requires compatible native sessions/tasks; see docs/compatibility.md",
                )
            )
        readiness = capabilities.get("readiness", {})
        if any(route.continuity for route in routes) and not readiness.get(
            "native", {}
        ).get("compaction"):
            checks.append(
                Check(
                    key + ":compaction",
                    "warning",
                    "Optional native compaction unavailable",
                )
            )
        if any("classic" in (route.voice, route.fallback) for route in routes):
            speech = readiness.get("speech", {})
            local_ok = all(speech.get(k) for k in ("ffmpeg", "transcription", "tts"))
            checks.append(
                Check(
                    key + ":speech",
                    "warning" if local_ok else "error",
                    "Local speech shims and ffmpeg available; provider credentials/audio not exercised"
                    if local_ok
                    else "Classic speech prerequisites missing or unreported; check ffmpeg and Hermes speech installation",
                )
            )
        return checks
    except ValueError:
        return [
            Check(
                key,
                "error",
                "Missing/short credentials or invalid capability response; check this endpoint configuration",
            )
        ]
    except httpx.HTTPStatusError as exc:
        detail = (
            "Authentication rejected; check API key and bridge secret"
            if exc.response.status_code in (401, 403)
            else f"Gateway returned HTTP {exc.response.status_code}; check API/plugin installation"
        )
        return [Check(key, "error", detail)]
    except (httpx.RequestError, TimeoutError):
        return [
            Check(
                key,
                "error",
                "Gateway unreachable or timed out; check endpoint URL and gateway service",
            )
        ]
    except Exception:
        # Neither response bodies nor exceptions from remote services belong in reports.
        return [
            Check(
                key,
                "error",
                "Incompatible gateway capabilities/profile; see docs/compatibility.md",
            )
        ]
    finally:
        if api is not None:
            await api.close()


async def routing_checks(*, offline=False):
    from .diagnostics import Check

    try:
        routing = RoutingConfig.load()
    except Exception:
        return [
            Check(
                "routing",
                "error",
                "Routing configuration invalid; run hfp-mcp route validate",
            )
        ]
    if not routing.enabled:
        return [Check("routing", "ok", "Automatic phone routing disabled")]
    routes = list(routing.numbers.values()) + (
        [routing.default] if routing.default else []
    )
    checks = [
        Check(
            "routing",
            "ok" if routes else "warning",
            f"{len(routes)} route(s); number region {routing.region}",
        )
    ]
    if any("gemini_live" in (route.voice, route.fallback) for route in routes):
        from .gemini_live import availability

        try:
            env = service_environment()
            result = availability(configured=True, environ=env)
            checks.append(
                Check(
                    "gemini",
                    "ok" if result["available"] else "error",
                    "Local Gemini prerequisites available; provider access/audio not exercised"
                    if result["available"]
                    else f"Gemini prerequisite missing: {result['reason']}",
                )
            )
            if any(r.continuity and r.voice == "gemini_live" for r in routes):
                from .settings import _bool

                enabled = all(
                    _bool(env.get(k), True)
                    for k in (
                        "HFP_GEMINI_INPUT_TRANSCRIPTION",
                        "HFP_GEMINI_OUTPUT_TRANSCRIPTION",
                    )
                )
                if not enabled:
                    checks.append(
                        Check(
                            "gemini:continuity",
                            "error",
                            "Continuity requires input and output transcription",
                        )
                    )
        except Exception:
            checks.append(
                Check(
                    "gemini", "error", "Invalid Gemini configuration or unavailable SDK"
                )
            )
    if offline:
        for name, endpoint in routing.endpoints.items():
            try:
                endpoint.secret()
                endpoint.secret(True)
                checks.append(
                    Check(
                        f"hermes:{name}",
                        "warning",
                        "Credentials present; gateway checks skipped (--offline)",
                    )
                )
            except ValueError:
                checks.append(
                    Check(
                        f"hermes:{name}",
                        "error",
                        "Missing or short endpoint credentials",
                    )
                )
        return checks
    results = await asyncio.gather(
        *(
            endpoint_checks(name, endpoint, [r for r in routes if r.endpoint == name])
            for name, endpoint in routing.endpoints.items()
        )
    )
    return checks + [check for result in results for check in result]
