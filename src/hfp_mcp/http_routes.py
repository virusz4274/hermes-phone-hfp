"""HTTP control routes with explicit access to the daemon runtime.

Bluetooth ownership and ASGI lifespan remain in server.py. Getters deliberately
resolve mutable controller/ledger objects at request time.
"""

from __future__ import annotations
import asyncio
import time
import httpx
from dataclasses import dataclass
from typing import Any, Callable
from starlette.responses import JSONResponse
from starlette.routing import Route
from .settings import RuntimeConfig
from .contracts import normalize_phone_number, validate_call_purpose


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
    callbacks: Callable = lambda: None


def control_routes(config: RuntimeConfig, runtime: HttpRuntime):
    workflow_executor = None

    def executions():
        nonlocal workflow_executor
        if workflow_executor is None:
            from .workflow_execution import WorkflowExecution
            workflow_executor = WorkflowExecution(runtime, config)
        return workflow_executor

    async def _workflow_start(request):
        try:
            return JSONResponse(await executions().start(await request.json()))
        except PermissionError:
            return JSONResponse({'ok': False, 'error': 'Phone workflow authority denied'}, status_code=403)
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            return JSONResponse({'ok': False, 'error': 'Phone workflow execution unavailable'}, status_code=409)

    async def _workflow_outcome(request):
        return JSONResponse(executions().outcome(request.path_params['attempt_id']))

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
        scheduler = runtime.callbacks()
        state["scheduled_callbacks"] = scheduler.list(include_terminal=False) if scheduler else []
        state['recent_callbacks'] = [r for r in scheduler.list() if r['status'] not in {'scheduled', 'firing'}][-10:] if scheduler else []
        return JSONResponse(state)

    async def _owner_lookup(request):
        from .owner_lookup import lookup
        from .routing import RoutingConfig
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {'source_endpoint', 'binding_id', 'run_id', 'owner_chat', 'query'}:
                raise ValueError('invalid owner lookup envelope')
            result = await lookup(RoutingConfig.load(), runtime.ledger(), body.get('source_endpoint'),
                {k: body[k] for k in ('binding_id', 'run_id', 'owner_chat') if k in body}, body.get('query', {}),
                transcripts_enabled=bool(config and config.full_transcripts))
            return JSONResponse(result)
        except PermissionError:
            return JSONResponse({'status': 'denied', 'message': 'Owner lookup denied'}, status_code=403)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({'status': 'error', 'message': 'Invalid or unavailable owner lookup'}, status_code=400)
        except (httpx.HTTPError, TimeoutError):
            return JSONResponse({'status': 'unavailable', 'message': 'Owner lookup service unavailable'}, status_code=503)

    async def _phone_callbacks(request):
        scheduler = runtime.callbacks()
        if scheduler is None:
            return JSONResponse({"error": "phone callback scheduler unavailable"}, status_code=409)
        if request.method == "GET":
            return JSONResponse({"callbacks": scheduler.list(), 'scheduler': 'hermes', 'legacy_history': True})
        if getattr(scheduler, 'retired', False):
            return JSONResponse({'ok': False, 'error': 'Daemon scheduling is retired; use Hermes hfp_phone_schedule_call'}, status_code=409)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            if not all(isinstance(body.get(key), str) for key in ("number", "request_id", "purpose")):
                raise ValueError("number, request_id and purpose must be text")
            controller = runtime.controller()
            if controller is None:
                return JSONResponse({'error': 'phone controller disabled'}, status_code=409)
            route, reason = controller.config.resolve_outbound(body['number'])
            if not route:
                return JSONResponse({'error': reason}, status_code=403)
            if config and config.self_number and normalize_phone_number(body['number'], controller.config.region) == normalize_phone_number(config.self_number, config.default_region):
                return JSONResponse({'error': 'outbound target matches the configured paired-handset number'}, status_code=403)
            callback = scheduler.schedule(
                request_id=body["request_id"], number=body["number"],
                purpose=body["purpose"], run_at=body.get("run_at"),
            )
            runtime.sync_state()
            connection = runtime.state.versioned_snapshot().get('connection', {}).get('state')
            return JSONResponse({"ok": True, "callback": callback,
                                 'warning': None if connection == 'connected' else
                                 'Callback saved. Bluetooth/HFP must be ready at the due time; delivery is not guaranteed.'})
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _callback_control(request):
        scheduler = runtime.callbacks()
        if scheduler is None:
            return JSONResponse({'error': 'phone callback scheduler unavailable'}, status_code=409)
        try:
            key = request.path_params['callback_id']
            if request.method == 'GET':
                return JSONResponse({'ok': True, 'callback': scheduler.get(key)})
            if request.path_params['action'] == 'cancel':
                return JSONResponse({'ok': True, 'callback': scheduler.cancel(key)})
            if request.path_params['action'] not in {'reminder', 'reminder-claim', 'reschedule', 'verify'}:
                raise ValueError('unknown callback action')
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('expected an object')
            if request.path_params['action'] == 'reminder-claim':
                return JSONResponse({'ok': True, 'acquired': scheduler.claim_reminder(key)})
            if request.path_params['action'] == 'verify':
                return JSONResponse({'ok': True, 'callback': scheduler.record_verification(
                    key, body.get('expected_revision'), body.get('verification'))})
            if request.path_params['action'] == 'reschedule':
                from datetime import datetime
                if not isinstance(body.get('run_at'), str):
                    raise ValueError('run_at must be ISO-8601 text with timezone')
                target = datetime.fromisoformat(body['run_at'].replace('Z', '+00:00'))
                if target.tzinfo is None:
                    raise ValueError('run_at must include a timezone')
                row = scheduler.begin_reschedule(key, request_id=body.get('request_id'),
                    expected_revision=body.get('expected_revision'), run_at=target.timestamp())
                if row['schedule_change']['status'] not in {'applied', 'failed'}:
                    row = await scheduler.reconcile(key)
                change = row['schedule_change']
                return JSONResponse({'ok': change['status'] == 'applied', 'callback': row, 'timezone': 'UTC',
                    'requested_time': body['run_at'], 'requested_timezone': str(target.tzinfo),
                    'verification': {'status': change['status'], 'requested_run_at': change['requested_run_at'],
                        'callback_run_at': row['run_at'], 'reminder_run_at': change['reminder_run_at'],
                        'reminder_missing': row['reminder_job_id'] is None}})
            scheduler.link_reminder(key, body.get('job_id'))
            return JSONResponse({'ok': True})
        except (TypeError, ValueError) as exc:
            from .callbacks import CallbackConflict
            if isinstance(exc, CallbackConflict):
                return JSONResponse({'error': str(exc)}, status_code=409)
            return JSONResponse({'error': str(exc)}, status_code=400)

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
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            purpose = validate_call_purpose(body.get("purpose", body.get("brief", "")))
            if not all(isinstance(body.get(key), str) for key in ("number", "request_id")):
                raise ValueError("number and request_id must be text")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            await runtime.start_call(
                body["number"], body["request_id"], purpose,
            )
        )

    async def _caller_notes(request):
        from .hermes_api import HermesAPI
        from .routing import RoutingConfig

        if request.path_params["action"] not in {"read", "update"}:
            return JSONResponse({"error": "unknown note operation"}, status_code=404)
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("number"), str):
                raise ValueError("number must be text")
            if set(body) - {"number", "notes", "expected_revision"}:
                raise ValueError("unexpected caller notes argument")
            update = request.path_params["action"] == "update"
            notes = body.get("notes")
            if update and (not isinstance(notes, str) or len(notes) > 8000):
                raise ValueError("notes must be text of at most 8000 characters")
            if update and type(body.get("expected_revision")) is not int:
                raise ValueError("expected_revision from a notes read is required")
            controller = runtime.controller()
            routing = controller.config if controller else RoutingConfig.load()
            number = normalize_phone_number(body["number"], routing.region)
            route, reason = routing.resolve_outbound(number)
            if not route:
                return JSONResponse({"error": reason}, status_code=403)
            if update and not routing.policies[route.policy].remember:
                return JSONResponse({"error": "persistent caller memory is disabled"}, status_code=403)
            api = HermesAPI(routing.endpoints[route.endpoint])
            try:
                return JSONResponse(await api.caller_notes(number, **({"notes": notes, "expected_revision": body["expected_revision"]} if update else {})))
            finally:
                await api.close()
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return JSONResponse({"error": "caller notes request rejected"}, status_code=code if code in {400, 403, 409} else 502)
        except httpx.TransportError:
            return JSONResponse({"error": "caller notes unavailable; update outcome may be unconfirmed"}, status_code=502)

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
        Route("/v1/phone/caller-notes/{action}", _caller_notes, methods=["POST"]),
        Route('/v1/phone/owner-lookup', _owner_lookup, methods=['POST']),
        Route("/v1/phone/recall", _phone_recall, methods=["POST"]),
        Route("/v1/phone", _phone_status, methods=["GET"]),
        Route("/v1/phone/transcripts", _transcripts, methods=["GET"]),
        Route("/v1/phone/transcripts/calls", _transcript_calls, methods=["GET"]),
        Route("/v1/phone/calls", _phone_start, methods=["POST"]),
        Route('/v1/phone/workflow-call', _workflow_start, methods=['POST']),
        Route('/v1/phone/workflow-outcome/{attempt_id}', _workflow_outcome, methods=['GET']),
        Route("/v1/phone/callbacks", _phone_callbacks, methods=["GET", "POST"]),
        Route('/v1/phone/callbacks/{callback_id}/{action}', _callback_control, methods=['POST']),
        Route('/v1/phone/callbacks/{callback_id}', _callback_control, methods=['GET']),
        Route("/v1/phone/approval", _phone_approval, methods=["POST"]),
        Route("/v1/state", _state_endpoint, methods=["GET"]),
        Route("/status", _legacy_status_endpoint, methods=["GET"]),
        Route("/healthz", _health_endpoint, methods=["GET"]),
        Route("/readyz", _ready_endpoint, methods=["GET"]),
    ]
    if runtime.audio_server() is not None:
        routes.extend(runtime.audio_server().routes())
    return routes
