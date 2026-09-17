"""Feature-driven HFP SLC tests using a scripted Audio Gateway."""

import asyncio

import pytest

from hfp_mcp.hfp.handshake import (
    HF_BRSF_FEATURES,
    HF_FEATURE_THREE_WAY,
    HFPHandshaker,
    HandshakeError,
)
from hfp_mcp.hfp.protocol import ATResponse, ATResult, ATUnsolicited
from hfp_mcp.state import CallState, ConnectionState, HFPState

CIND_DEF_PAYLOAD = '("call",(0,1)),("callsetup",(0-3)),("service",(0,1))'
CIND_VALS_PAYLOAD = "0,0,1"
CHLD_PAYLOAD = "0,1,1x,2,2x,3"


def _script(*, remote_features: int = 36, values: str = CIND_VALS_PAYLOAD):
    return [
        ("BRSF", [ATResult("+BRSF", str(remote_features)), ATResponse(True)]),
        ("CIND=?", [ATResult("+CIND", CIND_DEF_PAYLOAD), ATResponse(True)]),
        ("CIND?", [ATResult("+CIND", values), ATResponse(True)]),
        ("CMER", [ATResponse(True)]),
        ("CLIP", [ATResponse(True)]),
    ]


async def _setup_state() -> HFPState:
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state._at_event_queue = asyncio.Queue()
    state._at_cmd_queue = asyncio.Queue()
    return state


async def _mock_phone(
    state: HFPState,
    script,
    timeout: float = 2.0,
    received: list[str] | None = None,
) -> None:
    for command_fragment, response_events in script:
        command = await asyncio.wait_for(state._at_cmd_queue.get(), timeout=timeout)
        command_text = command.decode("ascii")
        if received is not None:
            received.append(command_text)
        assert command_fragment in command_text
        for event in response_events:
            state._at_event_queue.put_nowait(event)


async def _run_with_phone(
    state: HFPState,
    script,
    *,
    hf_features: int = HF_BRSF_FEATURES,
    timeout: float = 2.0,
) -> HFPHandshaker:
    handshaker = HFPHandshaker(
        state,
        timeout=timeout,
        hf_features=hf_features,
    )
    await asyncio.gather(handshaker.run(), _mock_phone(state, script))
    return handshaker


@pytest.mark.asyncio
async def test_hf_initiated_handshake_success():
    state = await _setup_state()
    await _run_with_phone(state, _script())

    assert state.connection_state == ConnectionState.CONNECTED
    assert state.remote_brsf == 36
    assert state.indicators["call"] == [1, 0]
    assert state.indicators["callsetup"] == [2, 0]
    assert state.indicators["service"] == [3, 1]


@pytest.mark.asyncio
async def test_initial_active_call_is_preserved_without_claiming_sco():
    state = await _setup_state()
    await _run_with_phone(state, _script(values="1,0,1"))

    assert state.call_state == CallState.ACTIVE
    assert state.audio_active is False


@pytest.mark.asyncio
async def test_initial_incoming_call_is_preserved():
    state = await _setup_state()
    await _run_with_phone(state, _script(values="0,1,1"))
    assert state.call_state == CallState.INCOMING


@pytest.mark.asyncio
async def test_handshake_error_on_timeout():
    state = await _setup_state()
    with pytest.raises(HandshakeError, match="timeout"):
        await HFPHandshaker(state, timeout=0.05).run()


@pytest.mark.asyncio
async def test_handshake_error_on_at_error():
    state = await _setup_state()
    script = [("BRSF", [ATResponse(False, "ERROR")])]
    with pytest.raises(HandshakeError, match="ERROR"):
        await _run_with_phone(state, script)


@pytest.mark.asyncio
async def test_default_features_do_not_query_chld():
    state = await _setup_state()
    received: list[str] = []
    script = _script(remote_features=1)
    handshaker = HFPHandshaker(state, timeout=2.0)
    await asyncio.gather(
        handshaker.run(),
        _mock_phone(state, script, received=received),
    )

    assert "BRSF" in received[0]
    assert any("CMER" in command for command in received)
    assert any("CLIP" in command for command in received)
    assert not any("CHLD" in command for command in received)


@pytest.mark.asyncio
async def test_chld_is_queried_only_when_both_roles_advertise_three_way():
    state = await _setup_state()
    features = HF_BRSF_FEATURES | HF_FEATURE_THREE_WAY
    script = _script(remote_features=1)
    script.insert(
        -1,
        ("CHLD", [ATResult("+CHLD", CHLD_PAYLOAD), ATResponse(True)]),
    )
    received: list[str] = []
    handshaker = HFPHandshaker(
        state,
        timeout=2.0,
        hf_features=features,
    )
    await asyncio.gather(
        handshaker.run(),
        _mock_phone(state, script, received=received),
    )
    assert any("CHLD" in command for command in received)


@pytest.mark.asyncio
async def test_ag_originated_brsf_does_not_reverse_command_roles():
    state = await _setup_state()
    unsolicited = ATUnsolicited(prefix="AT+BRSF=99", payload="")
    state._at_event_queue.put_nowait(unsolicited)
    received: list[str] = []
    handshaker = HFPHandshaker(state, timeout=2.0)
    await asyncio.gather(
        handshaker.run(),
        _mock_phone(state, _script(), received=received),
    )

    assert received[0] == f"AT+BRSF={HF_BRSF_FEATURES}\r"
    assert state.remote_brsf == 36
    assert await state._at_event_queue.get() == unsolicited


@pytest.mark.asyncio
async def test_disconnect_sentinel_fails_without_waiting_for_timeout():
    state = await _setup_state()

    async def disconnect_phone():
        await state._at_cmd_queue.get()
        state._at_event_queue.put_nowait(None)

    with pytest.raises(HandshakeError, match="closed"):
        await asyncio.gather(
            HFPHandshaker(state, timeout=2.0).run(),
            disconnect_phone(),
        )
