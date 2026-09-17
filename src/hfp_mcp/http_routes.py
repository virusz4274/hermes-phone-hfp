"""HTTP control routes with explicit access to the daemon runtime.

Bluetooth ownership and ASGI lifespan remain in server.py. Getters deliberately
resolve mutable controller/ledger objects at request time.
"""

from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable
from starlette.responses import JSONResponse
from starlette.routing import Route
from .settings import RuntimeConfig


@dataclass(frozen=True)
class HttpRuntime:
    state: Any
    sync_state: Callable
    controller: Callable
    ledger: Callable
    audio_server: Callable
    transcript: Callable
    transcripts: Callable
    start_call: Callable


def control_routes(config: RuntimeConfig, runtime: HttpRuntime):
    async def _state_endpoint(_request):
        runtime.sync_state()
        return JSONResponse(runtime.state.versioned_snapshot())

    async def _legacy_status_endpoint(_request):
        runtime.sync_state()
        return JSONResponse(
            runtime.state.versioned_snapshot(), headers={"Deprecation": "true"}
        )

    async def _health_endpoint(_request):
        runtime.sync_state()
        state = runtime.state.versioned_snapshot()
        return JSONResponse(
            {
                "ok": True,
                "ready": bool(
                    config.device_address
                    and state["health"].get("bluez") in {"profile_ready", "ok"}
                    and state["health"].get("http") == "ok"
                ),
                "schema_version": state["schema_version"],
                "server_instance_id": state["server_instance_id"],
                "health": state["health"],
            }
        )

    async def _ready_endpoint(_request):
        runtime.sync_state()
        state = runtime.state.versioned_snapshot()
        ready = bool(
            config.device_address
            and state["health"].get("bluez") in {"profile_ready", "ok"}
            and state["health"].get("http") == "ok"
        )
        return JSONResponse(
            {
                "ok": ready,
                "ready": ready,
                "schema_version": state["schema_version"],
                "server_instance_id": state["server_instance_id"],
                "health": state["health"],
                "reason": None
                if ready
                else "Bluetooth profile or enrolled phone is not ready",
            },
            status_code=200 if ready else 503,
        )

    async def _gateway_phone_tasks(action, **body):
        from .hermes_api import HermesAPI
        from .routing import RoutingConfig

        results = []
        for endpoint in RoutingConfig.load().endpoints.values():
            api = HermesAPI(endpoint)
            try:
                result = await api.phone_tasks("", "", action=action, **body)
                results.append({"profile": endpoint.profile, **result})
            except Exception:
                results.append({"profile": endpoint.profile, "status": "unavailable"})
            finally:
                await api.close()
        return results

    async def _phone_status(_request):
        state = (
            dict(runtime.controller().status)
            if runtime.controller()
            else {"enabled": False}
        )
        state["native_phone_tasks"] = await _gateway_phone_tasks("owner_status")
        return JSONResponse(state)

    async def _transcripts(request):
        try:
            result = runtime.transcript(
                request.query_params.get("call_id"),
                int(request.query_params.get("after_id", "0")),
                int(request.query_params.get("limit", "500")),
            )
        except ValueError:
            return JSONResponse({"error": "invalid pagination"}, status_code=400)
        return JSONResponse(result, status_code=200 if result.get("ok") else 403)

    async def _transcript_calls(_request):
        result = runtime.transcripts()
        return JSONResponse(result, status_code=200 if result.get("ok") else 403)

    async def _phone_start(request):
        body = await request.json()
        return JSONResponse(
            await runtime.start_call(
                str(body.get("number", "")), str(body.get("request_id", ""))
            )
        )

    async def _phone_approval(request):
        if runtime.controller() is None:
            return JSONResponse({"error": "phone controller disabled"}, status_code=409)
        try:
            body = await request.json()
            if not runtime.controller().call_id:
                results = await _gateway_phone_tasks(
                    "owner_approve",
                    request_id=body["request_id"],
                    choice=body["choice"],
                )
                successful = [r for r in results if r.get("status") != "unavailable"]
                if not successful:
                    raise ValueError("no matching approval")
                return JSONResponse(successful[0])
            return JSONResponse(
                await runtime.controller().approve(body["request_id"], body["choice"])
            )
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid or stale approval"}, status_code=409)

    async def _phone_recall(request):
        body = await request.json()
        c = runtime.controller()
        if body.get("run_id") and runtime.ledger() is not None:
            from .routing import RoutingConfig
            from .hermes_api import HermesAPI
            from .conversations import ConversationStore

            for endpoint in RoutingConfig.load().endpoints.values():
                api = HermesAPI(endpoint)
                try:
                    identity = await api.phone_tasks(
                        body.get("binding_id"),
                        "",
                        action="recall_authority",
                        run_id=body["run_id"],
                    )
                    with runtime.ledger()._lock:
                        row = (
                            runtime.ledger()
                            ._db.execute(
                                "SELECT persistent FROM phone_conversations WHERE id=? AND profile=? AND caller_id=?",
                                (
                                    identity["conversation_id"],
                                    identity["profile"],
                                    identity["caller_id"],
                                ),
                            )
                            .fetchone()
                        )
                    if row:
                        conversation = {
                            "id": identity["conversation_id"],
                            "profile": identity["profile"],
                            "caller_id": identity["caller_id"],
                            "persistent": bool(row[0]),
                        }
                        return JSONResponse(
                            await asyncio.to_thread(
                                ConversationStore(runtime.ledger()).recall,
                                conversation,
                                query=body.get("query", ""),
                                before_id=body.get("before_id"),
                                archived=bool(body.get("archived", False)),
                                message_id=body.get("message_id"),
                                offset=body.get("offset", 0),
                                chars=12000,
                                byte_limit=11000,
                            )
                        )
                except Exception:
                    pass
                finally:
                    await api.close()
            return JSONResponse({"error": "No authorized caller task"}, status_code=403)
        if (
            not c
            or not c.conversation
            or not c.request_binding
            or body.get("binding_id") != c.request_binding["session_id"]
            or c._binding_deadlines.get(c.request_binding["session_id"], 0)
            <= time.monotonic()
            or c.identity(c.snapshot())[:2] != (c.call_id, "active")
        ):
            return JSONResponse(
                {"error": "No matching active caller task"}, status_code=403
            )
        try:
            return JSONResponse(await c.conversation.recall(body))
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid recall request"}, status_code=400)

    routes = [
        Route("/v1/phone/recall", _phone_recall, methods=["POST"]),
        Route("/v1/phone", _phone_status, methods=["GET"]),
        Route("/v1/phone/transcripts", _transcripts, methods=["GET"]),
        Route("/v1/phone/transcripts/calls", _transcript_calls, methods=["GET"]),
        Route("/v1/phone/calls", _phone_start, methods=["POST"]),
        Route("/v1/phone/approval", _phone_approval, methods=["POST"]),
        Route("/v1/state", _state_endpoint, methods=["GET"]),
        Route("/status", _legacy_status_endpoint, methods=["GET"]),
        Route("/healthz", _health_endpoint, methods=["GET"]),
        Route("/readyz", _ready_endpoint, methods=["GET"]),
    ]
    if runtime.audio_server() is not None:
        routes.extend(runtime.audio_server().routes())
    return routes
