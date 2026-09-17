"""Tests for HFPState thread-safe transitions."""

import threading

import pytest
from hfp_mcp.state import CallDirection, CallState, ConnectionState, HFPState, SCOState


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


def test_versioned_snapshot_separates_call_from_physical_sco():
    s = HFPState()
    s.set_call_state(CallState.INCOMING)
    call_id = s.call_id
    s.set_remote_identity("+14155552671", source="clip")
    s.set_call_state(CallState.ACTIVE)
    state = s.versioned_snapshot()
    assert state["schema_version"] == "hfp.v1"
    assert state["call"]["id"] == call_id
    assert state["call"]["direction"] == CallDirection.INCOMING.value
    assert state["call"]["remote_number_source"] == "clip"
    assert state["call"]["remote_number_verified"] is True
    assert state["audio"]["sco_state"] == SCOState.DISCONNECTED.value


def test_audio_client_attachment_is_stream_scoped_and_resets_with_sco():
    state = HFPState()
    state.set_sco_state(
        SCOState.READY,
        owner="hermes_classic",
        stream_id="stream-current",
    )

    assert state.set_audio_client_attached(
        True, stream_id="stream-stale"
    ) is False
    assert state.set_audio_client_attached(
        True, stream_id="stream-current"
    ) is True
    assert state.versioned_snapshot()["audio"]["client_attached"] is True
    assert state.snapshot()["audio_client_attached"] is True

    state.set_sco_state(SCOState.DISCONNECTED)

    assert state.versioned_snapshot()["audio"]["client_attached"] is False


def test_audio_transport_metrics_are_exposed_and_reset_for_new_stream():
    state = HFPState()
    state.set_sco_state(
        SCOState.READY,
        owner="gemini_live",
        stream_id="stream-one",
    )
    state.set_audio_metrics(
        queue_ms=120.0,
        dropped_frames=2,
        queue_peak_ms=300.0,
        overflow_events=1,
        rejected_bytes=640,
        accepted_bytes=3200,
        consumed_bytes=1600,
        playback_controlled=True,
        playback_target_ms=160.0,
        playback_underrun_events=2,
        playback_underrun_ms=24.0,
        capture_queue_ms=40.0,
        capture_queue_peak_ms=100.0,
        capture_overflow_bytes=320,
    )

    audio = state.versioned_snapshot()["audio"]
    assert audio["queue_ms"] == 120.0
    assert audio["queue_peak_ms"] == 300.0
    assert audio["overflow_events"] == 1
    assert audio["rejected_bytes"] == 640
    assert audio["playback_controlled"] is True
    assert audio["playback_target_ms"] == 160.0
    assert audio["playback_underrun_events"] == 2
    assert audio["playback_underrun_ms"] == 24.0
    assert audio["capture_queue_ms"] == 40.0
    assert audio["capture_overflow_bytes"] == 320

    state.set_sco_state(
        SCOState.READY,
        owner="gemini_live",
        stream_id="stream-two",
    )
    audio = state.versioned_snapshot()["audio"]
    assert audio["queue_ms"] == 0.0
    assert audio["dropped_frames"] == 0
    assert audio["overflow_events"] == 0
    assert audio["playback_controlled"] is False
    assert audio["playback_underrun_events"] == 0
    assert audio["capture_overflow_bytes"] == 0


def test_stale_disconnect_cannot_clear_new_connection():
    class FakeSock:
        pass

    s = HFPState()
    old = s.set_connected("AA:BB:CC:DD:EE:01", FakeSock())
    new = s.set_connected("AA:BB:CC:DD:EE:02", FakeSock())
    assert new > old
    assert s.set_disconnected(old) is False
    assert s.connected_address == "AA:BB:CC:DD:EE:02"


def test_call_transition_compare_and_set_does_not_recreate_ended_call():
    s = HFPState()
    s.set_call_state(CallState.ACTIVE)
    call_id = s.call_id
    generation = s.call_generation
    s.set_call_state(CallState.IDLE)

    changed = s.transition_call_state(
        CallState.ENDING,
        expected_call_id=call_id,
        expected_generation=generation,
        allowed_states={CallState.ACTIVE},
    )

    assert changed is False
    assert s.call_state == CallState.IDLE
    assert s.call_id is None


def test_pending_outbound_identity_is_promoted_only_for_outgoing_call():
    state = HFPState()
    assert state.set_pending_outbound_identity(
        "+14155552671",
        caller_role="admin",
        expected_connection_generation=0,
        expected_call_generation=0,
    )
    assert state.versioned_snapshot()["call"]["remote_number_verified"] is False

    state.set_call_state(CallState.DIALING, direction=CallDirection.OUTGOING)

    call = state.versioned_snapshot()["call"]
    assert call["remote_number"] == "+14155552671"
    assert call["remote_number_source"] == "dialed"
    assert call["remote_number_verified"] is True
    assert call["caller_role"] == "admin"


def test_incoming_call_racing_pending_atd_drops_outbound_identity_and_role():
    state = HFPState()
    assert state.set_pending_outbound_identity(
        "+14155552671",
        caller_role="admin",
        expected_connection_generation=0,
        expected_call_generation=0,
    )

    state.set_call_state(CallState.INCOMING, direction=CallDirection.INCOMING)

    call = state.versioned_snapshot()["call"]
    assert call["remote_number"] is None
    assert call["caller_role"] == "unknown"


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
