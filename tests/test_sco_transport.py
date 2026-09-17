"""Focused lifecycle tests for the shared physical SCO transport."""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time

import pytest

import hfp_mcp.audio.sco as sco
from hfp_mcp.audio.sco import AudioManager, SCOAudioError


ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeSCOSocket:
    """Blocking fake with Linux SCO socket-option behavior."""

    def __init__(self, *, voice: int = sco._BT_VOICE_CVSD_16BIT, mtu: int = 64):
        self.voice = voice
        self.mtu = mtu
        self.closed = False
        self.timeout = None
        self.sent: queue.Queue[bytes] = queue.Queue()
        self.incoming: queue.Queue[object] = queue.Queue()
        self.sockopts = []
        self.bound = None
        self.peer = None
        self.backlog = None

    def setsockopt(self, level, option, value):
        self.sockopts.append((level, option, value))
        if level == sco._SOL_BLUETOOTH and option == sco._BT_VOICE:
            self.voice = struct.unpack("=H", value)[0]

    def getsockopt(self, level, option, size):
        if level == sco._SOL_BLUETOOTH and option == sco._BT_VOICE:
            return struct.pack("=H", self.voice)
        if level == sco._SOL_SCO and option == sco._SCO_OPTIONS:
            return struct.pack("=H", self.mtu)
        raise OSError("unknown fake option")

    def settimeout(self, timeout):
        self.timeout = timeout

    def bind(self, address):
        if not isinstance(address, (str, bytes)):
            raise OSError("bind(): wrong format")
        self.bound = address

    def connect(self, address):
        if not isinstance(address, (str, bytes)):
            raise OSError("connect(): wrong format")
        self.peer = address

    def listen(self, backlog):
        self.backlog = backlog

    def recv(self, _size):
        item = self.incoming.get(timeout=2)
        if isinstance(item, BaseException):
            raise item
        return item

    def send(self, data):
        self.sent.put(bytes(data))
        return len(data)

    def shutdown(self, _how):
        if not self.closed:
            self.incoming.put(OSError("closed"))

    def close(self):
        self.closed = True


class IgnoringVoiceSocket(FakeSCOSocket):
    def setsockopt(self, level, option, value):
        self.sockopts.append((level, option, value))


class FakeListener:
    def __init__(self, accepted):
        self.accepted = queue.Queue()
        for item in accepted:
            self.accepted.put(item)
        self.closed = False

    def accept(self):
        if self.closed:
            raise OSError("closed")
        try:
            return self.accepted.get_nowait()
        except queue.Empty:
            raise socket.timeout()

    def shutdown(self, _how):
        self.closed = True

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def test_cvsd_is_set_and_read_back_explicitly():
    sock = FakeSCOSocket(voice=0)

    assert sco._configure_cvsd(sock) == sco._BT_VOICE_CVSD_16BIT
    assert sock.sockopts == [
        (
            sco._SOL_BLUETOOTH,
            sco._BT_VOICE,
            struct.pack("=H", sco._BT_VOICE_CVSD_16BIT),
        )
    ]


def test_cvsd_mismatch_is_rejected():
    sock = IgnoringVoiceSocket(voice=0x0003)

    with pytest.raises(SCOAudioError, match="not using 16-bit CVSD"):
        sco._configure_cvsd(sock)


def test_outbound_socket_uses_direct_sco_addresses_and_verified_cvsd(monkeypatch):
    physical = FakeSCOSocket(voice=0)
    monkeypatch.setattr(sco, "_new_sco_socket", lambda: physical)

    connected = sco._SCOTransport(ADDRESS)._connect_sco(0.2)

    assert connected is physical
    assert physical.bound == sco._BDADDR_ANY.encode("ascii")
    assert physical.peer == ADDRESS.encode("ascii")
    assert physical.voice == sco._BT_VOICE_CVSD_16BIT
    assert physical.timeout is None


def test_connected_socket_cvsd_mismatch_fails_closed(monkeypatch):
    physical = IgnoringVoiceSocket(voice=0x0003)
    events = []
    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager(events.append)
    session = manager.create_session("call", ADDRESS)

    with pytest.raises(SCOAudioError, match="not using 16-bit CVSD"):
        session.start()

    assert physical.closed is True
    assert session.running is False
    assert manager.has_active_transport() is False
    assert events[-1].state == "failed"
    manager.remove_session("call")


def test_concurrent_logical_leases_share_one_physical_connection(monkeypatch):
    physical = FakeSCOSocket()
    establish_entered = threading.Event()
    allow_establish = threading.Event()
    calls = []

    def establish(_transport, _timeout, _stop_event):
        calls.append(1)
        establish_entered.set()
        assert allow_establish.wait(1)
        return physical, "outbound"

    monkeypatch.setattr(sco._SCOTransport, "_establish_sco", establish)
    manager = AudioManager()
    first = manager.create_session("first", ADDRESS)
    second = manager.create_session("second", ADDRESS)
    second_events = []
    second.add_health_callback(
        lambda event: second_events.append(event.state), replay=False
    )
    errors = []

    def start(session):
        try:
            session.start(1)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    one = threading.Thread(target=start, args=(first,))
    two = threading.Thread(target=start, args=(second,))
    one.start()
    assert establish_entered.wait(1)
    two.start()
    allow_establish.set()
    one.join(1)
    two.join(1)

    assert errors == []
    assert calls == [1]
    assert first._sock is physical
    assert second._sock is physical
    assert second_events[-1] == "connected"
    assert manager.has_active_transport() is True

    assert manager.remove_session("first") is True
    assert physical.closed is False
    assert second.running is True
    assert manager.remove_session("second") is True
    assert physical.closed is True
    assert manager.has_active_transport() is False


def test_bridge_fans_out_capture_and_mixes_playback(monkeypatch):
    physical = FakeSCOSocket()
    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager()
    first = manager.create_session("first", ADDRESS)
    second = manager.create_session("second", ADDRESS)
    first.start()
    second.start()

    # Two +20,000 samples saturate at signed-16 maximum in the shared TX frame.
    sample = (20_000).to_bytes(2, "little", signed=True)
    first.queue_playback(sample * 2)
    second.queue_playback(sample * 2)
    captured = b"\x01\x00\x02\x00"
    physical.incoming.put(captured)

    assert physical.sent.get(timeout=1) == b"\xff\x7f\xff\x7f"
    wait_until(lambda: first.pop_stream_frame(4) is not None)
    # The first polling call consumed first's frame; both leases received it.
    assert second.pop_stream_frame(4) == captured
    manager.stop_all()


def test_bridge_holds_controlled_pcm_until_jitter_target(monkeypatch):
    physical = FakeSCOSocket(mtu=64)
    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager()
    session = manager.create_session("call", ADDRESS)
    session.start()
    session.start_buffered_playback("generation-1")
    frame = b"\x22\x11" * (sco.STREAM_FRAME_BYTES // 2)
    for _ in range((sco.PLAYBACK_TARGET_BYTES // sco.STREAM_FRAME_BYTES) - 1):
        session.queue_playback(frame)

    physical.incoming.put(b"\x01\x00" * 32)
    assert physical.sent.get(timeout=1) == b"\x00" * 64

    session.queue_playback(frame)
    physical.incoming.put(b"\x02\x00" * 32)
    assert physical.sent.get(timeout=1) == frame[:64]
    metrics = session.media_metrics()
    assert metrics["playback_prebuffer_silence_bytes"] == 64
    assert metrics["playback_underrun_events"] == 0
    assert metrics["capture_received_bytes"] == 128
    manager.stop_all()


def test_controlled_playback_preserves_adjacent_utterance_boundaries(monkeypatch):
    physical = FakeSCOSocket()
    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager()
    session = manager.create_session("call", ADDRESS)
    session.start()

    session.start_buffered_playback("one")
    session.queue_playback(b"a" * 32)
    session.end_buffered_playback("one")
    session.start_buffered_playback("two")
    session.queue_playback(b"b" * sco.PLAYBACK_TARGET_BYTES)
    session.end_buffered_playback("two")

    assert session._take_playback_raw(64) == b"a" * 32
    assert session._take_playback_raw(64) == b"b" * 64
    metrics = session.media_metrics()
    assert metrics["playback_utterances_started"] == 2
    assert metrics["playback_utterances_completed"] == 1
    assert metrics["playback_underrun_events"] == 0
    manager.stop_all()


def test_inbound_sco_for_expected_phone_wins_connect_race(monkeypatch):
    accepted = FakeSCOSocket()
    listener = FakeListener([(accepted, ADDRESS.encode("ascii"))])
    transport = sco._SCOTransport(ADDRESS)

    monkeypatch.setattr(transport, "_open_listener", lambda: listener)

    def cancelled_outbound(_timeout, stop_event, race_cancel):
        while not stop_event.is_set() and not race_cancel.wait(0.01):
            pass
        raise SCOAudioError("cancelled")

    monkeypatch.setattr(transport, "_connect_sco", cancelled_outbound)
    winner, direction = transport._establish_sco(0.5, threading.Event())

    assert winner is accepted
    assert direction == "inbound"
    assert accepted.closed is False
    assert listener.closed is True


def test_unexpected_inbound_peer_is_closed_before_expected_peer(monkeypatch):
    foreign = FakeSCOSocket()
    expected = FakeSCOSocket()
    listener = FakeListener(
        [
            (foreign, b"11:22:33:44:55:66"),
            (expected, ADDRESS),
        ]
    )
    transport = sco._SCOTransport(ADDRESS)
    monkeypatch.setattr(transport, "_open_listener", lambda: listener)

    def cancelled_outbound(_timeout, stop_event, race_cancel):
        while not stop_event.is_set() and not race_cancel.wait(0.01):
            pass
        raise SCOAudioError("cancelled")

    monkeypatch.setattr(transport, "_connect_sco", cancelled_outbound)
    winner, direction = transport._establish_sco(0.5, threading.Event())

    assert foreign.closed is True
    assert winner is expected
    assert direction == "inbound"


def test_outbound_is_used_when_listener_is_unavailable(monkeypatch):
    outbound = FakeSCOSocket()
    transport = sco._SCOTransport(ADDRESS)
    monkeypatch.setattr(
        transport,
        "_open_listener",
        lambda: (_ for _ in ()).throw(OSError("listener unavailable")),
    )
    monkeypatch.setattr(
        transport,
        "_connect_sco",
        lambda _timeout, _stop: outbound,
    )

    winner, direction = transport._establish_sco(0.5, threading.Event())

    assert winner is outbound
    assert direction == "outbound"


def test_bridge_failure_marks_link_unhealthy_and_notifies(monkeypatch):
    physical = FakeSCOSocket()
    failed = threading.Event()
    events = []

    def health(event):
        events.append(event)
        if event.state == "failed":
            failed.set()

    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager(health)
    session = manager.create_session("call", ADDRESS)
    session.start()
    session.queue_playback(b"stale audio")
    physical.incoming.put(b"")

    assert failed.wait(1)
    assert session.running is False
    assert manager.has_active_transport() is False
    assert session.health()["reason"] == "Peer closed the SCO audio link"
    assert session.clear_playback() == 0
    with pytest.raises(RuntimeError, match="not running"):
        session.queue_playback(b"\x00\x00")
    assert [event.state for event in events][:2] == ["connecting", "connected"]
    assert events[-1].state == "failed"
    manager.stop_all()


def test_immediate_bridge_failure_cannot_be_overwritten_by_connected(monkeypatch):
    physical = FakeSCOSocket()
    physical.incoming.put(b"")
    events = []
    failed = threading.Event()

    def health(event):
        events.append(event.state)
        if event.state == "failed":
            failed.set()

    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager(health)
    session = manager.create_session("call", ADDRESS)
    try:
        session.start()
    except SCOAudioError:
        # If EOF wins the startup race, start() correctly refuses the lease.
        pass

    assert failed.wait(1)
    assert events[:2] == ["connecting", "connected"]
    assert events[-1] == "failed"
    assert session.running is False
    assert manager.has_active_transport() is False
    manager.stop_all()


def test_stale_bridge_cannot_clear_replacement_generation():
    transport = sco._SCOTransport(ADDRESS)
    old = FakeSCOSocket()
    replacement = FakeSCOSocket()
    with transport._condition:
        transport._generation = 2
        transport._state = "connected"
        transport._sock = replacement
        transport._voice_setting = sco._BT_VOICE_CVSD_16BIT

    transport._bridge_ended(1, old, "old generation failed")

    assert old.closed is True
    assert transport.socket is replacement
    assert transport.health_event().state == "connected"
    assert transport.health_event().generation == 2


def test_manager_bounds_leases_and_rejects_a_second_phone():
    manager = AudioManager(max_sessions=2)
    manager.create_session("one", ADDRESS)
    manager.create_session("two", ADDRESS.lower())

    with pytest.raises(ValueError, match="Maximum logical"):
        manager.create_session("three", ADDRESS)

    other_manager = AudioManager()
    other_manager.create_session("one", ADDRESS)
    with pytest.raises(SCOAudioError, match="one physical SCO phone"):
        other_manager.create_session("two", "11:22:33:44:55:66")


def test_playback_queue_rejects_overflow_without_corrupting_queued_audio(monkeypatch):
    physical = FakeSCOSocket()
    monkeypatch.setattr(
        sco._SCOTransport,
        "_establish_sco",
        lambda _transport, _timeout, _stop: (physical, "outbound"),
    )
    manager = AudioManager()
    session = manager.create_session("call", ADDRESS)
    session.start()

    session.queue_playback(b"a" * sco.PLAYBACK_MAX_BYTES)

    with pytest.raises(sco.SCOPlaybackBufferOverflow, match="refusing to overwrite"):
        session.queue_playback(b"new")

    with session._pb_lock:
        assert len(session._playback) == sco.PLAYBACK_MAX_BYTES
        assert session._playback[-3:] == b"aaa"
    metrics = session.media_metrics()
    assert metrics["playback_accepted_bytes"] == sco.PLAYBACK_MAX_BYTES
    assert metrics["playback_rejected_bytes"] == 3
    assert metrics["playback_overflow_bytes"] == 3
    assert metrics["playback_overflow_events"] == 1
    manager.stop_all()
