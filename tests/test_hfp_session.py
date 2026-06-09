"""Tests for active-session call-state dispatch."""

import asyncio

from hfp_mcp.hfp.session import ATEventDispatcher
from hfp_mcp.state import CallState, ConnectionState, HFPState


def test_ciev_callsetup_one_sets_incoming_call_state():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.indicators = {"callsetup": [2, 0]}

    dispatcher = ATEventDispatcher(state)
    dispatcher._handle_ciev("2,1")

    assert state.call_state == CallState.INCOMING
    assert state.indicators["callsetup"][1] == 1


def test_ciev_callsetup_zero_clears_incoming_call_state():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.INCOMING
    state.indicators = {"callsetup": [2, 1]}

    dispatcher = ATEventDispatcher(state)
    dispatcher._handle_ciev("2,0")

    assert state.call_state == CallState.IDLE
    assert state.indicators["callsetup"][1] == 0


def test_ciev_call_zero_notifies_call_ended_cleanup():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.ACTIVE
    state.audio_active = True
    state.indicators = {"call": [1, 1]}
    cleanup_calls = []

    dispatcher = ATEventDispatcher(state, lambda: cleanup_calls.append("cleanup"))
    dispatcher._handle_ciev("1,0")

    assert state.call_state == CallState.IDLE
    assert state.audio_active is False
    assert cleanup_calls == ["cleanup"]


def test_no_carrier_notifies_call_ended_cleanup():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.ACTIVE
    state.audio_active = True
    cleanup_calls = []

    dispatcher = ATEventDispatcher(state, lambda: cleanup_calls.append("cleanup"))
    dispatcher._handle_urc("NO CARRIER", "")

    assert state.call_state == CallState.IDLE
    assert state.audio_active is False
    assert cleanup_calls == ["cleanup"]


async def test_rfcomm_sentinel_notifies_call_ended_cleanup():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.call_state = CallState.ACTIVE
    state.audio_active = True
    state.sco_connected = True
    state._at_event_queue = asyncio.Queue()
    await state._at_event_queue.put(None)
    cleanup_calls = []

    dispatcher = ATEventDispatcher(state, lambda: cleanup_calls.append("cleanup"))
    await dispatcher.run()

    assert state.connection_state == ConnectionState.DISCONNECTED
    assert state.call_state == CallState.IDLE
    assert state.audio_active is False
    assert state.sco_connected is False
    assert cleanup_calls == ["cleanup"]
