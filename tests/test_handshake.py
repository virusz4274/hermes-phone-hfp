"""
Tests for the HFP handshaker using a concurrent mock-phone coroutine.

No real Bluetooth hardware needed.  A "mock phone" task watches the AT
command queue and feeds canned responses into the event queue, exactly
as a real phone would over the RFCOMM channel.
"""

import asyncio

import pytest
from hfp_mcp.hfp.handshake import HFPHandshaker, HandshakeError
from hfp_mcp.hfp.protocol import ATResponse, ATResult, ATUnsolicited
from hfp_mcp.state import ConnectionState, HFPState

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CIND_DEF_PAYLOAD = '("call",(0,1)),("callsetup",(0-3)),("service",(0,1))'
CIND_VALS_PAYLOAD = "0,0,1"
CHLD_PAYLOAD = "0,1,1x,2,2x,3"

# Map command fragment → list of events the phone sends in response
STANDARD_PHONE_SCRIPT = [
    ("BRSF",  [ATResult("+BRSF", "36"), ATResponse(success=True)]),
    ("CIND=?", [ATResult("+CIND", CIND_DEF_PAYLOAD), ATResponse(success=True)]),
    ("CIND?",  [ATResult("+CIND", CIND_VALS_PAYLOAD), ATResponse(success=True)]),
    ("CMER",   [ATResponse(success=True)]),
    ("CHLD",   [ATResult("+CHLD", CHLD_PAYLOAD), ATResponse(success=True)]),
]


async def _setup_state() -> HFPState:
    state = HFPState()
    state._asyncio_loop = asyncio.get_event_loop()
    state._at_event_queue = asyncio.Queue()
    state._at_cmd_queue = asyncio.Queue()
    return state


async def _mock_phone(state: HFPState, script, timeout: float = 2.0) -> None:
    """
    Simulate the Android phone: for each step in the script, wait for the
    corresponding AT command then push the canned response events.
    """
    for cmd_fragment, response_events in script:
        cmd_bytes = await asyncio.wait_for(state._at_cmd_queue.get(), timeout=timeout)
        assert cmd_fragment.encode() in cmd_bytes, (
            f"Expected command containing {cmd_fragment!r}, got {cmd_bytes!r}"
        )
        for event in response_events:
            state._at_event_queue.put_nowait(event)


async def _run_with_phone(state: HFPState, script, hs_timeout: float = 2.0) -> None:
    """Run handshaker and mock phone concurrently."""
    hs = HFPHandshaker(state, timeout=hs_timeout)
    await asyncio.gather(hs.run(), _mock_phone(state, script))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hf_initiated_handshake_success():
    state = await _setup_state()
    await _run_with_phone(state, STANDARD_PHONE_SCRIPT)

    assert state.connection_state == ConnectionState.CONNECTED
    assert state.remote_brsf == 36
    assert "call" in state.indicators
    assert "callsetup" in state.indicators
    assert "service" in state.indicators


@pytest.mark.asyncio
async def test_indicator_values_populated():
    state = await _setup_state()
    await _run_with_phone(state, STANDARD_PHONE_SCRIPT)

    # service indicator index=3, initial value=1
    assert state.indicators["service"] == [3, 1]
    # call index=1, initial value=0
    assert state.indicators["call"] == [1, 0]


@pytest.mark.asyncio
async def test_handshake_error_on_timeout():
    state = await _setup_state()
    hs = HFPHandshaker(state, timeout=0.05)   # 50 ms — fails fast
    with pytest.raises(HandshakeError, match="[Tt]imeout"):
        await hs.run()


@pytest.mark.asyncio
async def test_handshake_error_on_at_error():
    state = await _setup_state()
    # Phone immediately returns ERROR to AT+BRSF
    script = [("BRSF", [ATResponse(success=False, code="ERROR")])]
    with pytest.raises(HandshakeError):
        await _run_with_phone(state, script)


@pytest.mark.asyncio
async def test_commands_sent_in_order():
    state = await _setup_state()

    received: list[str] = []

    async def recording_phone():
        for cmd_fragment, response_events in STANDARD_PHONE_SCRIPT:
            cmd_bytes = await asyncio.wait_for(state._at_cmd_queue.get(), timeout=2.0)
            received.append(cmd_bytes.decode("ascii"))
            for event in response_events:
                state._at_event_queue.put_nowait(event)

    hs = HFPHandshaker(state, timeout=2.0)
    await asyncio.gather(hs.run(), recording_phone())

    assert any("BRSF" in c for c in received), "AT+BRSF not sent"
    assert any("CIND=?" in c for c in received), "AT+CIND=? not sent"
    assert any("CIND?" in c for c in received), "AT+CIND? not sent"
    assert any("CMER" in c for c in received), "AT+CMER not sent"
    assert any("CHLD" in c for c in received), "AT+CHLD not sent"

    brsf_idx = next(i for i, c in enumerate(received) if "BRSF" in c)
    cind_idx = next(i for i, c in enumerate(received) if "CIND=?" in c)
    assert brsf_idx < cind_idx, "AT+BRSF must be sent before AT+CIND=?"


@pytest.mark.asyncio
async def test_ag_initiated_handshake():
    """Samsung-style: phone sends AT+BRSF=36 before we do anything."""
    state = await _setup_state()

    # Pre-seed an AG-initiated event (arrives before any command from us)
    state._at_event_queue.put_nowait(
        ATUnsolicited(prefix="AT+BRSF=36", payload="")
    )

    # In AG-initiated mode the handshaker sends its raw "+BRSF:N\r\nOK\r\n" reply
    # first (via _send_raw → at_cmd_queue), then proceeds with CIND/CMER/CHLD.
    ag_script = [
        ("+BRSF",  []),             # consume the raw +BRSF reply, no new events needed
        ("CIND=?", [ATResult("+CIND", CIND_DEF_PAYLOAD), ATResponse(success=True)]),
        ("CIND?",  [ATResult("+CIND", CIND_VALS_PAYLOAD), ATResponse(success=True)]),
        ("CMER",   [ATResponse(success=True)]),
        ("CHLD",   [ATResult("+CHLD", CHLD_PAYLOAD), ATResponse(success=True)]),
    ]

    hs = HFPHandshaker(state, timeout=2.0)
    await asyncio.gather(hs.run(), _mock_phone(state, ag_script))

    assert state.connection_state == ConnectionState.CONNECTED
    assert state.remote_brsf == 36
