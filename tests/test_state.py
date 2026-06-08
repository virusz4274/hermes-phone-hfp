"""Tests for HFPState thread-safe transitions."""

import threading

import pytest
from hfp_mcp.state import CallState, ConnectionState, HFPState


def test_initial_state():
    s = HFPState()
    assert s.connection_state == ConnectionState.DISCONNECTED
    assert s.call_state == CallState.IDLE
    assert s.audio_active is False


def test_set_connected():
    import socket
    s = HFPState()
    # Use a dummy socket-like object (no real socket needed for state test)
    class FakeSock:
        pass
    s.set_connected("AA:BB:CC:DD:EE:FF", FakeSock())
    assert s.connection_state == ConnectionState.HANDSHAKING
    assert s.connected_address == "AA:BB:CC:DD:EE:FF"


def test_set_handshake_complete():
    s = HFPState()
    s.connection_state = ConnectionState.HANDSHAKING
    s.set_handshake_complete()
    assert s.connection_state == ConnectionState.CONNECTED


def test_set_disconnected_resets_everything():
    s = HFPState()
    s.connection_state = ConnectionState.CONNECTED
    s.call_state = CallState.ACTIVE
    s.audio_active = True
    s.connected_address = "AA:BB:CC:DD:EE:FF"
    s.indicators = {"call": [1, 1]}
    s.set_disconnected()
    assert s.connection_state == ConnectionState.DISCONNECTED
    assert s.call_state == CallState.IDLE
    assert s.audio_active is False
    assert s.connected_address is None
    assert s.indicators == {}


def test_snapshot_returns_copy():
    s = HFPState()
    snap = s.snapshot()
    assert snap["connection"] == "disconnected"
    assert snap["call_state"] == "idle"
    assert snap["audio_active"] is False


def test_concurrent_state_mutations():
    """Ensure lock prevents data races under concurrent writes."""
    s = HFPState()
    errors = []

    def writer():
        for _ in range(500):
            try:
                s.set_call_state(CallState.ACTIVE)
                s.set_call_state(CallState.IDLE)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
