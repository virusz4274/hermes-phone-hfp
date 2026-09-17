"""Tests for active-session call-state dispatch."""

import asyncio
import errno
import socket
import threading

import pytest

from hfp_mcp.hfp.protocol import (
    ATResponse,
    ATResult,
    CallDirection,
    parse_clcc,
)
from hfp_mcp.hfp.session import (
    ATCommandBroker,
    ATCommandError,
    ATCommandTimeout,
    ATEventDispatcher,
    RFCOMMThread,
)
from hfp_mcp.state import CallState, ConnectionState, HFPState


async def _blocking_call(function, /, *args):
    """Run socket/thread joins without pytest's Python 3.13 executor leak."""

    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def invoke():
        try:
            value = function(*args)
        except BaseException as exc:  # pragma: no cover - assertion plumbing.
            loop.call_soon_threadsafe(result.set_exception, exc)
        else:
            loop.call_soon_threadsafe(result.set_result, value)

    threading.Thread(target=invoke, daemon=True).start()
    return await result


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


def test_ciev_call_active_does_not_claim_sco_readiness():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.indicators = {"call": [1, 0], "callsetup": [2, 0]}

    dispatcher = ATEventDispatcher(state)
    dispatcher._handle_ciev("1,1")

    assert state.call_state == CallState.ACTIVE
    assert state.audio_active is False


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


def test_delayed_non_idle_urc_cannot_regress_ending_call():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    state.set_call_state(CallState.ENDING)
    dispatcher = ATEventDispatcher(state)

    dispatcher._handle_urc("RING", "")

    assert state.call_state == CallState.ENDING


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


def test_clip_and_clcc_preserve_direction_and_caller_metadata():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    dispatcher = ATEventDispatcher(state)

    dispatcher._handle_urc("RING", "")
    dispatcher._handle_urc(
        "+CLIP", '"+14155550100",145,,,"Alice"'
    )
    dispatcher.apply_clcc_entries(
        [parse_clcc('1,1,4,0,0,"+14155550100",145,"Alice"')]
    )

    assert state.call_state == CallState.INCOMING
    assert dispatcher.call_info.direction == CallDirection.INCOMING
    assert dispatcher.call_info.number == "+14155550100"
    assert dispatcher.call_info.name == "Alice"
    assert dispatcher.call_info.generation == 1
    call = state.versioned_snapshot()["call"]
    assert call["remote_number_source"] == "clcc_unvalidated"
    assert call["remote_number_verified"] is False


def test_call_end_callback_receives_ended_generation_and_identity():
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    ended = []
    dispatcher = ATEventDispatcher(state, on_call_ended_info=ended.append)
    dispatcher._handle_urc("RING", "")
    dispatcher._handle_urc("+CLIP", '"123",129,,,"Alice"')
    dispatcher._handle_urc("NO CARRIER", "")

    assert ended[0].generation == 1
    assert ended[0].direction == CallDirection.INCOMING
    assert ended[0].number == "123"
    assert ended[0].name == "Alice"


async def _broker_state() -> HFPState:
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state._at_event_queue = asyncio.Queue()
    state._at_cmd_queue = asyncio.Queue()
    return state


@pytest.mark.asyncio
async def test_command_broker_serializes_concurrent_transactions():
    state = await _broker_state()
    broker = ATCommandBroker(state, default_timeout=1.0)
    dispatcher = ATEventDispatcher(state, broker=broker)
    dispatcher_task = asyncio.create_task(dispatcher.run())

    first = asyncio.create_task(broker.execute("AT+ONE\r"))
    second = asyncio.create_task(broker.execute("AT+TWO\r"))
    assert await state._at_cmd_queue.get() == b"AT+ONE\r"
    await asyncio.sleep(0)
    assert state._at_cmd_queue.empty()
    state._at_event_queue.put_nowait(ATResponse(True))
    await first
    assert await state._at_cmd_queue.get() == b"AT+TWO\r"
    state._at_event_queue.put_nowait(ATResponse(True))
    await second

    state._at_event_queue.put_nowait(None)
    await dispatcher_task


@pytest.mark.asyncio
async def test_command_broker_demuxes_urc_from_result_and_terminal_response():
    state = await _broker_state()
    state.indicators = {"callsetup": [2, 0], "call": [1, 0]}
    broker = ATCommandBroker(state, default_timeout=1.0)
    dispatcher = ATEventDispatcher(state, broker=broker)
    dispatcher_task = asyncio.create_task(dispatcher.run())

    transaction = asyncio.create_task(
        broker.execute("AT+TEST\r", expected_prefixes=("+TEST",))
    )
    assert await state._at_cmd_queue.get() == b"AT+TEST\r"
    state._at_event_queue.put_nowait(ATResult("+CIEV", "2,1"))
    state._at_event_queue.put_nowait(ATResult("+TEST", "42"))
    state._at_event_queue.put_nowait(ATResponse(True))

    result = await transaction
    assert result.first("+TEST").payload == "42"
    assert state.call_state == CallState.INCOMING
    state._at_event_queue.put_nowait(None)
    await dispatcher_task


@pytest.mark.asyncio
async def test_command_broker_surfaces_error_and_clears_pending():
    state = await _broker_state()
    broker = ATCommandBroker(state, default_timeout=1.0)
    dispatcher = ATEventDispatcher(state, broker=broker)
    dispatcher_task = asyncio.create_task(dispatcher.run())

    transaction = asyncio.create_task(broker.execute("AT+FAIL\r"))
    await state._at_cmd_queue.get()
    state._at_event_queue.put_nowait(ATResponse(False, "+CME ERROR: 3"))
    with pytest.raises(ATCommandError, match="CME ERROR"):
        await transaction
    assert broker.pending is False
    state._at_event_queue.put_nowait(None)
    await dispatcher_task


@pytest.mark.asyncio
async def test_command_broker_timeout_and_cancellation_clear_pending():
    state = await _broker_state()
    broker = ATCommandBroker(state, default_timeout=0.02)
    with pytest.raises(ATCommandTimeout):
        await broker.execute("AT+TIMEOUT\r")
    assert broker.pending is False
    assert broker.terminal_quarantined is True

    # A late terminal has no transaction ID, so the broker must not send the
    # next command until that response is consumed.
    with pytest.raises(ATCommandError, match="terminal response unresolved"):
        await broker.execute("AT+CANCEL\r", timeout=1.0)
    assert await state._at_cmd_queue.get() == b"AT+TIMEOUT\r"
    assert state._at_cmd_queue.empty()
    assert broker.handle_event(ATResponse(True)) is True
    assert broker.terminal_quarantined is False

    task = asyncio.create_task(broker.execute("AT+CANCEL\r", timeout=1.0))
    assert await state._at_cmd_queue.get() == b"AT+CANCEL\r"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending is False
    assert broker.terminal_quarantined is True
    assert broker.handle_event(ATResponse(True)) is True
    assert broker.terminal_quarantined is False


@pytest.mark.asyncio
async def test_command_broker_quarantine_expires_when_no_terminal_arrives():
    state = await _broker_state()
    broker = ATCommandBroker(
        state,
        default_timeout=0.005,
        terminal_quarantine_seconds=0.02,
    )
    with pytest.raises(ATCommandTimeout):
        await broker.execute("AT+TIMEOUT\r")
    assert await state._at_cmd_queue.get() == b"AT+TIMEOUT\r"
    assert broker.terminal_quarantined is True

    await asyncio.sleep(0.03)
    assert broker.terminal_quarantined is False

    recovered = asyncio.create_task(broker.execute("AT+RECOVERED\r", timeout=1.0))
    assert await state._at_cmd_queue.get() == b"AT+RECOVERED\r"
    assert broker.handle_event(ATResponse(True)) is True
    assert (await recovered).response.success is True
    assert broker.terminal_quarantined is False


@pytest.mark.asyncio
async def test_command_broker_rejects_command_injection():
    state = await _broker_state()
    broker = ATCommandBroker(state)
    with pytest.raises(ValueError):
        await broker.execute("ATD123\rAT+CHUP\r")


@pytest.mark.asyncio
async def test_rfcomm_thread_owns_io_closes_and_remains_joinable():
    state = await _broker_state()
    owned, peer = socket.socketpair()
    state.rfcomm_socket = owned
    thread = RFCOMMThread(state, generation=7)
    thread.start()
    try:
        peer.sendall(b"\r\nRING\r\n")
        event = await asyncio.wait_for(state._at_event_queue.get(), timeout=1.0)
        assert event.prefix == "RING"

        await state._at_cmd_queue.put(b"ATA\r")
        assert await _blocking_call(peer.recv, 64) == b"ATA\r"
    finally:
        thread.stop()
        await _blocking_call(thread.join, 2.0)
        peer.close()
    assert not thread.is_alive()
    assert owned.fileno() == -1
    assert await asyncio.wait_for(state._at_event_queue.get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_rfcomm_connection_timeout_closes_socket_and_notifies_dispatcher():
    state = await _broker_state()

    class LostConnection:
        closed = False

        def recv(self, size):
            raise OSError(errno.ETIMEDOUT, "Bluetooth connection timed out")

        def shutdown(self, how):
            pass

        def close(self):
            self.closed = True

    owned = LostConnection()
    thread = RFCOMMThread(state, owned, generation=9)
    thread.start()
    try:
        assert await asyncio.wait_for(state._at_event_queue.get(), timeout=2) is None
        await _blocking_call(thread.join, 2)
        assert owned.closed
        assert not thread.is_alive()
    finally:
        thread.stop()
        await _blocking_call(thread.join, 2)


@pytest.mark.asyncio
async def test_rfcomm_thread_treats_inherited_idle_timeout_as_transient():
    state = await _broker_state()

    class IdleTimeoutSocket:
        def __init__(self):
            self.recv_calls = 0
            self.released = threading.Event()
            self.closed = False

        def recv(self, _size):
            self.recv_calls += 1
            if self.recv_calls == 1:
                raise OSError(errno.EAGAIN, "inherited RFCOMM idle timeout")
            if self.recv_calls == 2:
                return b"\r\nRING\r\n"
            self.released.wait(timeout=1.0)
            return b""

        def sendall(self, _data):
            return None

        def shutdown(self, _how):
            self.released.set()

        def close(self):
            self.closed = True
            self.released.set()

    owned = IdleTimeoutSocket()
    thread = RFCOMMThread(state, owned, generation=9)
    thread.start()
    try:
        event = await asyncio.wait_for(state._at_event_queue.get(), timeout=1.0)
        assert event.prefix == "RING"
        assert owned.recv_calls >= 2
        assert thread.is_alive()
    finally:
        thread.stop()
        await _blocking_call(thread.join, 2.0)

    assert owned.closed is True
    assert not thread.is_alive()
    assert await asyncio.wait_for(state._at_event_queue.get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_rfcomm_thread_never_posts_old_events_to_replacement_queue():
    state = await _broker_state()
    state.connection_generation = 1
    old_events = state._at_event_queue
    old_commands = state._at_cmd_queue
    owned, peer = socket.socketpair()
    state.rfcomm_socket = owned
    thread = RFCOMMThread(state, generation=1)
    thread.start()
    try:
        state.connection_generation = 2
        state._at_event_queue = asyncio.Queue()
        state._at_cmd_queue = asyncio.Queue()
        peer.sendall(b"\r\nRING\r\n")
        event = await asyncio.wait_for(old_events.get(), timeout=1.0)
        assert event.prefix == "RING"
        assert state._at_event_queue.empty()
        assert old_commands.empty()
    finally:
        thread.stop()
        await _blocking_call(thread.join, 2.0)
        peer.close()
    assert await asyncio.wait_for(old_events.get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_rfcomm_thread_can_start_from_immutable_connection_queues():
    state = await _broker_state()
    context_events = asyncio.Queue()
    context_commands = asyncio.Queue()
    replacement_events = asyncio.Queue()
    replacement_commands = asyncio.Queue()
    state._at_event_queue = replacement_events
    state._at_cmd_queue = replacement_commands
    owned, peer = socket.socketpair()
    thread = RFCOMMThread(
        state,
        owned,
        generation=1,
        loop=asyncio.get_running_loop(),
        event_queue=context_events,
        command_queue=context_commands,
    )
    thread.start()
    try:
        peer.sendall(b"\r\nRING\r\n")
        event = await asyncio.wait_for(context_events.get(), timeout=1.0)
        assert event.prefix == "RING"
        assert replacement_events.empty()

        await context_commands.put(b"ATA\r")
        assert await _blocking_call(peer.recv, 64) == b"ATA\r"
        assert replacement_commands.empty()
    finally:
        thread.stop()
        await _blocking_call(thread.join, 2.0)
        peer.close()
