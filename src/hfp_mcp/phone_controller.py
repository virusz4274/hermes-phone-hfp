"""One call controller inside the canonical daemon; no gateway platform emulation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing, suppress

import httpx

from .contracts import CALLER_BINDING_TTL_SECONDS
from .hermes_api import HermesAPI
from .routing import RoutingConfig

log = logging.getLogger(__name__)


class PhoneController:
    def __init__(
        self,
        config: RoutingConfig,
        *,
        snapshot,
        answer,
        end,
        make_voice,
        connect=None,
        api_factory=HermesAPI,
        transcript_sink=None,
        timing_sink=None,
        ledger=None,
    ):
        self.config = config
        self.ledger = ledger
        self.conversation = None
        self.transcript_sink, self.timing_sink = transcript_sink, timing_sink
        self._ending_call_id = None
        self.snapshot, self.answer, self.end = snapshot, answer, end
        self.make_voice, self.connect, self.api_factory = (
            make_voice,
            connect,
            api_factory,
        )
        self.task = None
        self.call_task = None
        self.heartbeat_task = None
        self.call_id = None
        self.api = None
        self.binding = None
        self.request_binding = None
        self._binding_deadlines = {}
        self.route = None
        self.history = []
        self.work_started = False
        self.voice = None
        self.status = {"enabled": True, "state": "idle"}
        self._seen = None
        self._arrived = 0.0
        self._number = None
        self._lock = asyncio.Lock()
        self._authority_lock = asyncio.Lock()
        self._next_connect = 0.0
        self.suspended = 0
        self.excluded_call_id = None

    def start(self):
        self.task = asyncio.create_task(self.watch(), name="hfp-phone-controller")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        await self.finish()

    @staticmethod
    def identity(state):
        call = state.get("call", {})
        number = (
            call.get("remote_number") if call.get("remote_number_verified") else None
        )
        return call.get("id"), call.get("state"), number

    async def watch(self):
        while True:
            try:
                if self.suspended:
                    await asyncio.sleep(0.2)
                    continue
                state = self.snapshot()
                call_id, phase, number = self.identity(state)
                if call_id and call_id == self.excluded_call_id:
                    await asyncio.sleep(0.2)
                    continue
                self.excluded_call_id = None
                if self.call_id and self.call_task and self.call_task.done():
                    # Do not reopen authority on a failed call if HFP hangup fails.
                    failed_call = self.call_id
                    with suppress(Exception):
                        await self.end(failed_call)
                    await self.finish()
                    self.excluded_call_id = failed_call
                    continue
                if self.call_id and (
                    self.call_id != call_id
                    or phase not in {"incoming", "ringing", "dialing", "active"}
                ):
                    await self.finish()
                if self.call_id and number and number != self._number:
                    # Never load another caller's context into an existing voice session.
                    await self.end(self.call_id)
                    await self.finish()
                    continue
                if call_id != self._seen:
                    self._seen, self._arrived = call_id, time.monotonic()
                if not call_id or phase not in {"incoming", "active"}:
                    if (
                        self.config.auto_reconnect
                        and self.connect
                        and time.monotonic() >= self._next_connect
                    ):
                        if state.get("connection", {}).get("state") == "disconnected":
                            self._next_connect = time.monotonic() + 30
                            await self.connect()
                    await asyncio.sleep(0.2)
                    continue
                if not self.call_id:
                    if not number and time.monotonic() - self._arrived < 2:
                        await asyncio.sleep(0.2)
                        continue
                    route, reason = self.config.resolve(number)
                    if not route:
                        self.status = {
                            "enabled": True,
                            "state": "declined",
                            "reason": reason,
                            "call_id": call_id,
                        }
                        await self.end(call_id)
                        await asyncio.sleep(0.5)
                        continue
                    if phase == "incoming" and not self.config.auto_answer:
                        await asyncio.sleep(0.2)
                        continue
                    self.call_id, self._number = call_id, number
                    self.call_task = asyncio.create_task(
                        self.serve(call_id, number, route), name=f"hfp-call-{call_id}"
                    )
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Phone controller failed; call authority is being revoked"
                )
                self.status = {
                    "enabled": True,
                    "state": "failed",
                    "reason": "controller_error",
                }
                if self.call_id:
                    with suppress(Exception):
                        await self.end(self.call_id)
                await self.finish()
                await asyncio.sleep(1)

    async def serve(self, call_id, number, route):
        started = time.monotonic()
        try:
            self._ending_call_id = None
            self.route = route
            self.history = []
            self.work_started = False
            endpoint = self.config.endpoints[route.endpoint]
            self.status = {
                "enabled": True,
                "state": "connecting",
                "call_id": call_id,
                "profile": endpoint.profile,
                "policy": route.policy,
            }
            self.api = self.api_factory(endpoint)
            capabilities = await self.api.check()
            if route.continuity and not ((capabilities or {}).get("conversation_sessions") and (capabilities or {}).get("parallel_phone_tasks")):
                raise RuntimeError("Hermes phone plugin does not support conversation sessions")
            api_ready_ms = round((time.monotonic() - started) * 1000)
            binding_started = time.monotonic()
            self.binding = await self.api.bind(
                call_id, number, route.endpoint, route.policy
            )
            self._binding_deadlines[self.binding["session_id"]] = binding_started + CALLER_BINDING_TTL_SECONDS
            if self.call_id != call_id:
                return
            self.heartbeat_task = asyncio.create_task(
                self.heartbeat(), name="hfp-call-authority"
            )
            self.status["session_id"] = self.binding["session_id"]
            if self.identity(self.snapshot())[1] == "incoming":
                await self.answer(call_id)
            deadline = time.monotonic() + 30
            while self.identity(self.snapshot())[1] != "active":
                if time.monotonic() >= deadline:
                    raise RuntimeError("call did not become active")
                await asyncio.sleep(0.1)
            if route.continuity:
                if self.ledger is None:
                    raise RuntimeError("conversation storage unavailable")
                from .conversations import ConversationStore
                from .phone_conversation import PhoneConversation
                self.conversation = PhoneConversation(self, ConversationStore(self.ledger))
                await self.conversation.start()
            context = await self.conversation.context() if self.conversation else self.voice_context()
            try:
                self.voice = self.make_voice(route.voice, self)
                await self.voice.start(call_id, context)
            except Exception:
                if self.voice:
                    await self.voice.stop()
                if (
                    self.work_started
                    or route.voice != "gemini_live"
                    or route.fallback != "classic"
                ):
                    raise
                # Startup only: no automatic switch or action replay mid-call.
                self.voice = self.make_voice("classic", self)
                await self.voice.start(call_id, context)
            self.status.update(state="ready", voice=self.voice.name)
            self.record_timing("voice_ready", {"api_ready_ms": api_ready_ms,
                               "total_setup_ms": round((time.monotonic() - started) * 1000)})
            expiry = (
                time.monotonic() + self.config.policies[route.policy].max_minutes * 60
            )
            while time.monotonic() < expiry:
                current_id, phase, _ = self.identity(self.snapshot())
                if current_id != call_id or phase != "active" or self._ending_call_id == call_id:
                    return
                if not self.voice.healthy():
                    # SCO can close just before the HFP hangup event reaches us.
                    await asyncio.sleep(0.25)
                    current_id, phase, _ = self.identity(self.snapshot())
                    if current_id != call_id or phase != "active" or self._ending_call_id == call_id:
                        return
                    if self.voice.healthy():
                        continue
                    raise RuntimeError("voice backend stopped")
                await asyncio.sleep(0.2)
            await self.end(call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Phone session failed")
            self.status.update(state="failed", reason="session_failed")
            with suppress(Exception):
                await self.end(call_id)

    async def _renew_binding(self, binding):
        sid = binding["session_id"]
        deadline = self._binding_deadlines[sid] - 0.5
        while True:
            started = time.monotonic()
            remaining = deadline - started
            if remaining <= 0:
                raise TimeoutError("caller binding renewal deadline exceeded")
            try:
                # Agent construction can briefly block the gateway event loop.
                # Wait only inside the last confirmed lease, never past expiry.
                await asyncio.wait_for(self.api.request(
                    "POST", f"v1/hfp/bindings/{sid}/renew", bridge=True,
                ), timeout=remaining)
                self._binding_deadlines[sid] = started + CALLER_BINDING_TTL_SECONDS
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise  # Authentication failure or revoked/expired binding.
            except httpx.TransportError:
                pass
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))

    async def heartbeat(self):
        try:
            while self.call_id:
                await asyncio.sleep(2)
                async with self._authority_lock:
                    bindings = [b for b in (self.binding, self.request_binding) if b]
                    tasks = [asyncio.create_task(self._renew_binding(b)) for b in bindings]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                error += f":{exc.response.status_code}"
            log.error("Phone authority renewal failed call=%s error=%s", self.call_id, error)
            self.record_timing("authority_lost", {"error": error})
            self.status.update(state="failed", reason="caller_authority_lost", authority_error=error)
            if self.call_task:
                self.call_task.cancel()
            if self.voice:
                with suppress(Exception):
                    await self.voice.stop()
            if self.call_id:
                with suppress(Exception):
                    await self.end(self.call_id)

    async def events(self, text: str, request_id: str, *, on_started=None):
        if self.conversation:
            # Classic speech also uses the gateway registry. It cannot bypass
            # continued jobs or race their native session history on a callback.
            engine, api, binding = self.conversation, self.api, self.binding
            conversation_id, generation = engine.conversation["id"], engine.generation
            await engine.flush()
            recent = await asyncio.to_thread(engine.store.recall, engine.conversation, chars=12000)
            row = await api.phone_tasks(binding["session_id"], conversation_id, submit=True,
                text=json.dumps({"caller_request": text, "recent_phone_dialogue": recent["messages"]}, ensure_ascii=False),
                request_id=request_id, relationship="new", continue_after_call=False)
            if not row.get("task_id"):
                yield {"type": "result", "status": "failed", "output": row.get("message", "Task was not admitted.")}
                return
            from .phone_tasks import TERMINAL
            announced = False
            while self.conversation is engine and engine.generation == generation and self.call_id:
                status = await api.phone_tasks(binding["session_id"], conversation_id, action="status", task_id=row["task_id"])
                row = status["tasks"][0]
                if row.get("run_id") and not announced:
                    announced = True
                    if on_started: on_started(row)
                if row.get("approval"):
                    yield {"type": "approval.request", **row["approval"]}
                if row["status"] in TERMINAL:
                    yield {"type": "result", "status": row["status"], "output": row.get("message", "")}
                    return
                await asyncio.sleep(.5)
            return
        async with self._lock:
            generation = self.conversation.generation if self.conversation else None
            if self.conversation and on_started is None:
                # Classic STT/TTS runs directly through Hermes, but still needs
                # spoken context saved by Gemini on earlier calls.
                await self.conversation.flush()
                recent = await asyncio.to_thread(self.conversation.store.recall, self.conversation.conversation, chars=12000)
                text = json.dumps({"caller_request": text, "recent_phone_dialogue": recent["messages"]}, ensure_ascii=False)
            started = time.monotonic()
            timing = {"request_id": request_id, "outcome": "error"}
            api, binding, call_id = self.api, self.binding, self.call_id
            if not api or not binding or not call_id:
                raise PermissionError("call ended")
            self.work_started = True
            # Separate execution authority prevents a cancelled run from using the
            # next run's lease. The caller's notes and bounded call history persist.
            binding_started = time.monotonic()
            child = await api.bind(
                call_id, self._number, self.route.endpoint, self.route.policy,
                **({"parent_binding_id": binding["session_id"]} if self.conversation else {}),
            )
            self._binding_deadlines[child["session_id"]] = binding_started + CALLER_BINDING_TTL_SECONDS
            self.request_binding = child
            timing["binding_ms"] = round((time.monotonic() - started) * 1000)
            revoked = False

            async def revoke():
                nonlocal revoked
                async with self._authority_lock:
                    if not revoked:
                        try:
                            await api.revoke(child["session_id"])
                            self._binding_deadlines.pop(child["session_id"], None)
                            revoked = True
                            self.request_binding = None
                        except Exception:
                            self.status.update(
                                state="failed", reason="request_authority_unavailable"
                            )
                            if self.call_task:
                                self.call_task.cancel()
                            raise

            try:
                async with aclosing(
                    api.events(
                        session_id=self.conversation.conversation["hermes_session"] if self.conversation else child["session_id"],
                        caller_id=binding["caller_id"],
                        text=text,
                        request_id=f"hfp-{binding['session_id']}-" +
                            (self.conversation.conversation["id"] + "-" if self.conversation else "") + request_id,
                        history=None if self.conversation else list(self.history),
                        revoke=revoke,
                        **({"binding_id": child["session_id"], "on_started": on_started} if self.conversation else {}),
                    )
                ) as events:
                    async for event in events:
                        elapsed = round((time.monotonic() - started) * 1000)
                        timing.setdefault("first_event_ms", elapsed)
                        if event.get("type") == "message.delta":
                            timing.setdefault("first_text_ms", elapsed)
                        if str(event.get("type", "")).startswith("tool."):
                            timing.setdefault("first_tool_event_ms", elapsed)
                        if self.call_id != call_id or (self.conversation and self.conversation.generation != generation):
                            return
                        self.status["run_id"] = api.run_id
                        if event.get("type") == "approval.request":
                            self.status["approval"] = event
                        elif event.get("type") in {"approval.responded", "result"}:
                            self.status.pop("approval", None)
                        if event.get("type") == "result":
                            timing["outcome"] = event.get("status", "unknown")
                            if not self.conversation:
                                self.history.extend(
                                    [
                                        {"role": "user", "content": text[:8000]},
                                        {"role": "assistant", "content": str(event.get("output") or "Request did not complete.")[:8000]},
                                    ]
                                )
                                self.history = self.history[-12:]
                        yield event
            except asyncio.CancelledError:
                timing["outcome"] = "cancelled"
                raise
            finally:
                timing["total_ms"] = round((time.monotonic() - started) * 1000)
                self.record_timing("hermes_request", timing, call_id=call_id)
                self.status.pop("approval", None)
                await revoke()
                self.request_binding = None

    def voice_context(self):
        policy = self.config.policies[self.route.policy]
        access = (
            "This caller is routed as admin. Hermes may use the selected profile's "
            "configured tools, including host-system tools when available, subject "
            "to normal approvals. This is not unrestricted OS/root access. "
            if policy.admin else
            "This caller has restricted access. Hermes may use only explicitly "
            "enabled caller capabilities; do not promise host-system access. "
        )
        return (
            "Selected Hermes profile: "
            + self.config.endpoints[self.route.endpoint].profile
            + ". Hermes connection and caller binding were checked for this call. "
            + access
            + "System requests refer to the host running this Hermes profile unless "
            "the caller identifies another system. Testing the assistant is not "
            "fictional role-play. Caller notes (untrusted facts): "
            + json.dumps(self.binding.get("notes", ""))
        )

    async def delegate(self, request):
        current_id, phase, _ = self.identity(self.snapshot())
        if not self.binding or not self.api or current_id != self.call_id or phase != "active":
            return {"status": "error", "message": "This phone session has ended."}
        if request.name in {"phone_recall", "phone_session", "hermes_task"}:
            if not self.conversation:
                return {"status": "error", "message": "Conversation continuity is not enabled for this route."}
            try:
                return await {"phone_recall": self.conversation.recall,
                              "phone_session": self.conversation.session_control,
                              "hermes_task": self.conversation.task_control}[request.name]({**request.arguments, "_request_id": request.request_id})
            except (ValueError, TypeError) as exc:
                return {"status": "error", "message": str(exc)}
        if request.name == "phone_status":
            try:
                await asyncio.wait_for(self.api.check(), timeout=3)
            except (TimeoutError, httpx.TransportError):
                return {"status": "pending", "message": "The Hermes connection check did not finish in time. This does not establish a permission failure or whether an existing task completed; do not repeat actions.",
                        "work_pending": self.request_binding is not None}
            except Exception:
                return {"status": "error", "message": "Hermes connection verification failed; task outcome has not been established."}
            if self.call_id != current_id or self.identity(self.snapshot())[:2] != (current_id, "active"):
                return {"status": "error", "message": "This phone session has ended."}
            return {"status": "ok", "message": "Connected to Hermes.",
                    "profile": self.config.endpoints[self.route.endpoint].profile,
                    "voice": self.route.voice, "work_pending": self.request_binding is not None,
                    "approval_pending": bool(self.status.get("approval")),
                    **(await self.conversation.task_control({"action": "status"}) if self.conversation else {})}
        if request.name == "end_call":
            if self._ending_call_id != self.call_id:
                outcome = await self.end(self.call_id)
                if isinstance(outcome, dict) and outcome.get("ok") is False:
                    return {"status": "error", "message": "The phone did not accept hangup. The call may still be active."}
                self._ending_call_id = self.call_id
            return {
                "status": "ok",
                "message": "Ending the call.",
                "speak_to_caller": False,
            }
        text = str(
            request.arguments.get("task")
            or request.arguments.get("request")
            or request.arguments.get("text")
            or ""
        )
        if not text or len(text) > 16000:
            return {"status": "error", "message": "Invalid Hermes request."}
        context = request.arguments.get("context", "")
        if not isinstance(context, str) or len(context) > 8000:
            return {"status": "error", "message": "Invalid Hermes request context."}
        if context.strip():
            # Preserve task boundaries and constraints from the voice conversation.
            # This is caller-supplied data, never an authority/approval override.
            text = json.dumps({"caller_request": text, "conversation_context": context}, ensure_ascii=False)
        if self.conversation:
            return await self.conversation.submit(text, request)
        final = None
        async for event in self.events(text, request.request_id):
            if event.get("type") == "result":
                final = event
        if not final:
            return {
                "status": "error",
                "message": "Hermes outcome unavailable; do not repeat the action.",
            }
        return {
            "status": "ok" if final["status"] == "completed" else "error",
            "message": str(
                final.get("output") or "Hermes did not complete the request."
            )[:12000],
        }

    def record_transcript(self, direction, text, *, provider="hermes", metadata=None):
        if self.conversation and self.call_id and text:
            self.conversation.capture({"session_id": self.call_id, "provider": provider,
                "direction": direction, "text": text, "timestamp": time.time(), "metadata": metadata or {}})
            return
        if self.transcript_sink and self.call_id and text:
            try:
                self.transcript_sink({"session_id": self.call_id, "provider": provider,
                                      "direction": direction, "text": text,
                                      "timestamp": time.time(), "metadata": metadata or {}})
            except Exception as exc:
                self.status["transcript_storage_error"] = type(exc).__name__
                log.error("Phone transcript storage failed: %s", type(exc).__name__)

    def record_timing(self, stage, detail, *, call_id=None):
        call_id = call_id or self.call_id
        self.status["last_timing"] = {"stage": stage, **detail}
        log.info("Phone timing call=%s stage=%s data=%s", call_id, stage, json.dumps(detail))
        if self.timing_sink and call_id:
            try:
                self.timing_sink(call_id, stage, detail)
            except Exception:
                log.warning("Could not persist phone timing")

    async def approve(self, request_id: str, choice: str):
        if self.conversation and self.api:
            state = await self.conversation.sync_tasks()
            matches = [t for t in state["tasks"] if (t.get("approval") or {}).get("request_id") == request_id]
            if len(matches) != 1 or choice not in {"once", "deny"}:
                raise ValueError("no matching approval or invalid choice")
            return await self.api.phone_tasks(self.binding["session_id"], self.conversation.conversation["id"],
                action="approve", task_id=matches[0]["task_id"], request_id=request_id, choice=choice)
        if (
            choice not in {"once", "deny"}
            or not self.api
            or not self.status.get("approval")
        ):
            raise ValueError("no matching approval or invalid choice")
        pending = self.status["approval"]
        if pending.get("request_id") != request_id:
            raise ValueError("approval request mismatch")
        result = await self.api.request(
            "POST",
            f"v1/runs/{self.api.run_id}/approval",
            json={"request_id": request_id, "choice": choice},
        )
        self.status.pop("approval", None)
        return result

    async def finish(self):
        # Revoke before cancelling model work. The gateway stop API is cooperative.
        api, binding = self.api, self.binding
        self.call_id = None
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
            self.heartbeat_task = None
        if api:
            for active in (binding, self.request_binding):
                if active:
                    with suppress(Exception):
                        await api.revoke(active["session_id"])
        if self.call_task:
            self.call_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.call_task
            self.call_task = None
        if self.voice:
            with suppress(Exception):
                await self.voice.stop()
        if self.conversation:
            await self.conversation.close()
            self.conversation = None
        if api:
            with suppress(Exception):
                await api.stop()
            await api.close()
        self.voice = self.api = self.binding = None
        self._binding_deadlines.clear()
        if self.status.get("state") != "failed":
            self.status = {"enabled": True, "state": "idle"}
