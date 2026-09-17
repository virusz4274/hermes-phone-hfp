import asyncio
import time
from types import SimpleNamespace

from hfp_mcp.gemini_live import GeminiLiveManager, GeminiRequest, _function_declarations


def direct_manager(handler):
    async def noop(*args):
        return {"ok": True}

    return GeminiLiveManager(
        ensure_stream=noop, clear_playback=noop, hangup=noop,
        allowed_tools={"ask_hermes", "phone_status", "end_call"},
        request_handler=handler,
    )


def request(rid, name="ask_hermes"):
    return GeminiRequest(
        request_id=rid, function_call_id=rid, name=name,
        arguments={"task": "check RAM"} if name == "ask_hermes" else {},
        created_at=time.time(), session_id="call-1",
    )


def test_routed_status_tool_is_exposed_without_widening_legacy_tools():
    declarations = _function_declarations({"phone_status", "ask_hermes", "end_call"})
    assert {d["name"] for d in declarations} == {"phone_status", "ask_hermes", "end_call"}
    assert "phone_status" not in {d["name"] for d in _function_declarations()}


async def test_direct_dispatch_bypasses_poll_queue_and_cancellation_reaches_handler():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def noop(*args):
        return {"ok": True}

    manager = GeminiLiveManager(
        ensure_stream=noop,
        clear_playback=noop,
        hangup=noop,
        allowed_tools={"ask_hermes", "end_call"},
        request_handler=handler,
    )
    request = GeminiRequest(
        request_id="req-1",
        function_call_id="req-1",
        name="ask_hermes",
        arguments={"task": "book"},
        created_at=time.time(),
        session_id="call-1",
    )
    assert manager.add_request(request)
    await entered.wait()
    assert manager._requests.empty()
    assert not manager.add_request(request)
    manager.cancel_request("req-1")
    await asyncio.wait_for(cancelled.wait(), 1)
    await manager._close_provider_session()
    assert manager._pending == {}
    assert {t["name"] for t in _function_declarations({"ask_hermes", "end_call"})} == {
        "ask_hermes",
        "end_call",
    }


async def test_status_and_hangup_remain_available_during_agent_work():
    entered = {name: asyncio.Event() for name in ("ask_hermes", "phone_status", "end_call")}

    async def handler(item):
        entered[item.name].set()
        await asyncio.Event().wait()

    manager = direct_manager(handler)
    try:
        assert manager.add_request(request("work"))
        await asyncio.wait_for(entered["ask_hermes"].wait(), 1)
        duplicate = request("extra-work")
        assert not manager.add_request(duplicate)
        assert duplicate.stale_reason == "request_busy"
        for name in ("phone_status", "end_call"):
            assert manager.add_request(request(name, name))
            await asyncio.wait_for(entered[name].wait(), 1)
            assert not manager.add_request(request(name + "-duplicate", name))
        assert len(manager._pending) == 3
    finally:
        await manager._close_provider_session()


async def test_replacement_waits_for_cancelled_run_cleanup_without_queue_rejection():
    entered, draining, release, replacement = (asyncio.Event() for _ in range(4))
    # Represents the controller lock: new authority cannot start while the
    # cancelled request is still revoking its binding/stopping its run.
    lock = asyncio.Lock()

    async def handler(item):
        async with lock:
            if item.request_id == "old":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    draining.set()
                    await release.wait()
            else:
                replacement.set()
                await asyncio.Event().wait()

    manager = direct_manager(handler)
    try:
        assert manager.add_request(request("old"))
        await asyncio.wait_for(entered.wait(), 1)
        await manager._handle_tool_cancellations(SimpleNamespace(
            tool_call_cancellation=SimpleNamespace(ids=["old"])))
        await asyncio.wait_for(draining.wait(), 1)
        assert manager.add_request(request("new"))
        await asyncio.sleep(0)
        assert not replacement.is_set()
        assert manager._known_request_states["old"] == "cancelled"
        release.set()
        await asyncio.wait_for(replacement.wait(), 1)
        assert "old" not in manager._pending
    finally:
        release.set()
        await manager._close_provider_session()


async def test_busy_call_is_not_executed_when_replayed_after_response_cache_eviction():
    calls, responses = [], []

    async def handler(item):
        calls.append(item.request_id)
        await asyncio.Event().wait()

    manager = direct_manager(handler)

    async def send(rid, name, payload):
        responses.append((rid, payload))

    manager._send_function_response = send
    response = SimpleNamespace(tool_call=SimpleNamespace(function_calls=[
        SimpleNamespace(id="busy", name="ask_hermes", args={"task": "install"})]))
    try:
        assert manager.add_request(request("running"))
        await asyncio.sleep(0)
        await manager._handle_tool_calls(response)
        assert responses[-1][1]["status"] == "pending"
        assert "not started" in responses[-1][1]["message"]
        manager.cancel_request("running")
        await asyncio.gather(*list(manager._direct_requests.values()), return_exceptions=True)
        manager._tool_response_outbox.clear()
        await manager._handle_tool_calls(response)
        await asyncio.sleep(0)
        assert calls == ["running"]
        assert "already resolved" in responses[-1][1]["message"]
    finally:
        await manager._close_provider_session()
