"""Tests for active-session call-state dispatch."""

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
