import httpx
import pytest

from hfp_mcp.hermes_api import REQUIRED, HermesAPI
from hfp_mcp.routing import Endpoint


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("TEST_API_TOKEN", "a" * 40)
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "b" * 40)
    return Endpoint(
        "default", "http://localhost:8642", "TEST_API_TOKEN", "TEST_BRIDGE_TOKEN"
    )


async def test_runs_retry_exact_identity_then_consume_native_events(endpoint):
    creates = []

    def handler(request):
        path = request.url.path
        if path == "/v1/runs":
            creates.append(request)
            if len(creates) == 1:
                raise httpx.ReadError("connection lost after submission")
            return httpx.Response(202, json={"run_id": "run-1"})
        if path.endswith("/events"):
            return httpx.Response(
                200,
                text='data: {"event":"message.delta","delta":"Done."}\n\ndata: {"event":"run.completed"}\n\n',
            )
        return httpx.Response(200, json={"status": "completed", "output": "Done."})

    api = HermesAPI(endpoint, transport=httpx.MockTransport(handler))
    events = [
        event
        async for event in api.events(
            session_id="hfp-a", caller_id="caller-a", text="book", request_id="req-a"
        )
    ]
    assert creates[0].content == creates[1].content
    assert (
        creates[0].headers["Idempotency-Key"]
        == creates[1].headers["Idempotency-Key"]
        == "req-a"
    )
    assert events[0] == {
        "event": "message.delta",
        "delta": "Done.",
        "type": "message.delta",
    }
    assert events[-1]["status"] == "completed"
    await api.close()


async def test_wrong_profile_bridge_rejected(endpoint):
    def handler(request):
        if request.url.path.endswith("hfp/capabilities"):
            assert request.headers["X-HFP-Bridge-Token"] == "b" * 40
            return httpx.Response(200, json={"version": 1, "profile": "wrong"})
        return httpx.Response(200, json={"features": {key: True for key in REQUIRED}})

    api = HermesAPI(endpoint, transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="profile mismatch"):
        await api.check()
    await api.close()


async def test_disconnected_event_stream_reattaches_without_new_run(endpoint):
    counts = {"creates": 0, "events": 0}

    def handler(request):
        if request.url.path == "/v1/runs":
            counts["creates"] += 1
            return httpx.Response(202, json={"run_id": "run-1"})
        if request.url.path.endswith("/events"):
            counts["events"] += 1
            raise httpx.ReadError("dropped stream")
        return httpx.Response(200, json={"status": "completed", "output": "Done"})

    api = HermesAPI(endpoint, transport=httpx.MockTransport(handler))
    events = [
        event
        async for event in api.events(
            session_id="hfp-a", caller_id="a", text="book", request_id="request"
        )
    ]
    assert counts["creates"] == 1
    assert events[-1]["output"] == "Done"
    await api.close()


async def test_early_consumer_close_stops_exact_run(endpoint):
    stopped = []

    def handler(request):
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-exact"})
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200, text='data: {"event":"message.delta","delta":"Working"}\n\n'
            )
        if request.url.path.endswith("/stop"):
            stopped.append(request.url.path)
            return httpx.Response(200, json={"status": "stopping"})
        raise AssertionError(request.url.path)

    api = HermesAPI(endpoint, transport=httpx.MockTransport(handler))
    events = api.events(
        session_id="hfp-a", caller_id="a", text="book", request_id="req-a"
    )
    await anext(events)
    await events.aclose()
    assert stopped == ["/v1/runs/run-exact/stop"]
    await api.close()
