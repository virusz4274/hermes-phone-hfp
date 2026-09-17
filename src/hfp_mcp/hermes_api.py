"""Hermes native Runs API client. Transport retries never invent another action ID."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import AsyncIterator

import httpx

from .routing import Endpoint
from .hermes_compat import REQUIRED_RUN_FEATURES

TERMINAL = {"completed", "failed", "cancelled"}
REQUIRED = REQUIRED_RUN_FEATURES


class HermesAPI:
    def __init__(self, endpoint: Endpoint, *, transport=None):
        self.endpoint = endpoint
        self.http = httpx.AsyncClient(
            base_url=endpoint.url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {endpoint.secret()}"},
            timeout=httpx.Timeout(15, read=60),
            transport=transport,
        )
        self.run_id: str | None = None

    async def request(self, method: str, path: str, *, bridge: bool = False, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if bridge:
            headers["X-HFP-Bridge-Token"] = self.endpoint.secret(True)
        response = await self.http.request(
            method, path.lstrip("/"), headers=headers, **kwargs
        )
        response.raise_for_status()
        return response.json()

    async def check(self) -> dict:
        capabilities = await self.request("GET", "v1/capabilities")
        missing = REQUIRED - {
            k for k, v in capabilities.get("features", {}).items() if v
        }
        if missing:
            raise RuntimeError(
                "Hermes gateway lacks required Runs API capabilities: "
                + ", ".join(sorted(missing))
            )
        bridge = await self.request("GET", "v1/hfp/capabilities", bridge=True)
        if bridge.get("version") != 1 or bridge.get("profile") != self.endpoint.profile:
            raise RuntimeError("phone bridge version or profile mismatch")
        return bridge

    async def bind(
        self, call_id: str, number: str | None, endpoint: str, policy: str, *, parent_binding_id=None
    ) -> dict:
        return await self.request(
            "POST",
            "v1/hfp/bindings",
            bridge=True,
            json={
                "call_id": call_id,
                "number": number,
                "endpoint": endpoint,
                "policy": policy,
                **({"parent_binding_id": parent_binding_id} if parent_binding_id else {}),
            },
        )

    async def revoke(self, session_id: str):
        await self.request("DELETE", f"v1/hfp/bindings/{session_id}", bridge=True)

    async def stop(self, run_id=None):
        target = run_id or self.run_id
        if target:
            await self.request("POST", f"v1/runs/{target}/stop", json={})

    async def phone_tasks(self, binding_id, conversation_id, *, submit=False, **body):
        return await self.request("POST", "v1/hfp/tasks/" + ("submit" if submit else "control"), bridge=True,
            json={"binding_id": binding_id, "conversation_id": conversation_id, **body})

    async def create_session(self, binding_id, conversation_id):
        return await self.request("POST", "v1/hfp/sessions", bridge=True,
                                  json={"binding_id": binding_id, "conversation_id": conversation_id})

    async def session_control(self, binding_id, session_id, action):
        return await self.request("DELETE" if action == "delete" else "POST",
            f"v1/hfp/sessions/{session_id}", bridge=True,
            json={"binding_id": binding_id, "action": action},
            **({"timeout": httpx.Timeout(15, read=300)} if action == "compact" else {}))

    async def steer(self, run_id, text):
        return await self.request("POST", f"v1/runs/{run_id}/steer", json={"input": text})

    async def events(
        self,
        *,
        session_id: str,
        caller_id: str,
        text: str,
        request_id: str,
        history=None,
        revoke=None,
        binding_id=None,
        on_started=None,
    ) -> AsyncIterator[dict]:
        payload = {"input": text, "session_id": session_id}
        if history:
            payload["conversation_history"] = history
        headers = {
            "Idempotency-Key": request_id,
            "X-Hermes-Session-Key": f"hfp:{self.endpoint.profile}:{caller_id}",
        }
        if binding_id:
            headers["X-HFP-Binding"] = binding_id
            headers["X-HFP-Bridge-Token"] = self.endpoint.secret(True)

        async def submit():
            for attempt in range(2):
                try:
                    return await self.request(
                        "POST", "v1/runs", json=payload, headers=headers
                    )
                except httpx.TransportError:
                    if attempt:
                        raise

        finished = False
        self.run_id = None
        launch = asyncio.create_task(submit())
        try:
            try:
                run = await asyncio.shield(launch)
            except asyncio.CancelledError:
                if revoke:
                    await asyncio.shield(revoke())
                # Capture a late acknowledgement so the exact admitted run can be stopped.
                with suppress(Exception):
                    run = await launch
                    self.run_id = run["run_id"]
                raise
            self.run_id = run["run_id"]
            if on_started:
                on_started(run)
            for reconnect in range(3):
                try:
                    async with self.http.stream(
                        "GET", f"v1/runs/{self.run_id}/events"
                    ) as response:
                        response.raise_for_status()
                        data, event_name = [], ""
                        async for line in response.aiter_lines():
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                            elif line.startswith("data:"):
                                data.append(line[5:].strip())
                            elif not line and data:
                                raw = "\n".join(data)
                                data = []
                                if raw != "[DONE]":
                                    event = json.loads(raw)
                                    event.setdefault(
                                        "type", event.get("event") or event_name
                                    )
                                    yield event
                except (httpx.TransportError, httpx.HTTPStatusError):
                    pass
                state = await self.request("GET", f"v1/runs/{self.run_id}")
                if state.get("status") in TERMINAL:
                    finished = True
                    yield {"type": "result", **state}
                    return
                # Reattach to the existing run; never resubmit the user action.
                await asyncio.sleep(0.25 * (reconnect + 1))
            raise RuntimeError(
                "Hermes event stream unavailable; run outcome requires reconciliation"
            )
        finally:
            try:
                if revoke:
                    await asyncio.shield(revoke())
            finally:
                if not launch.done():
                    with suppress(Exception):
                        late = await asyncio.shield(launch)
                        self.run_id = late["run_id"]
                if not finished:
                    with suppress(Exception):
                        await asyncio.shield(self.stop())
            # Keep the run ID for diagnostics/reconciliation even after stop.

    async def close(self):
        await self.http.aclose()
