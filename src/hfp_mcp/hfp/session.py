"""RFCOMM ownership, serialized AT transactions, and active-call dispatch."""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..config import AT_TERMINAL_QUARANTINE_SECONDS, AT_TIMEOUT_SECONDS
from ..state import CallState, HFPState
from .protocol import (
    ATParser,
    ATProtocolError,
    ATResponse,
    ATResult,
    ATUnsolicited,
    CLCCEntry,
    CLCCStatus,
    CLIPInfo,
    CMD_CLCC,
    CallDirection,
    ParsedAT,
    URC_CIEV,
    URC_CLIP,
    parse_clcc,
    parse_clip,
)

log = logging.getLogger(__name__)

MAX_AT_COMMAND_BYTES = 512


class ATCommandError(RuntimeError):
    """The audio gateway rejected a command or the transaction was invalid."""

    def __init__(self, command: str, code: str) -> None:
        self.command = command
        self.code = code
        super().__init__(f"AT command {command.strip()!r} failed: {code}")


class ATCommandTimeout(ATCommandError):
    """No terminal response arrived before the command deadline."""


class ATConnectionClosed(ATCommandError):
    """RFCOMM closed while a command was pending."""


@dataclass(frozen=True)
class ATCommandResult:
    """One complete command transaction."""

    command: str
    response: ATResponse
    results: tuple[ATResult, ...] = ()

    def matching(self, prefix: str) -> tuple[ATResult, ...]:
        wanted = prefix.upper()
        return tuple(result for result in self.results if result.prefix == wanted)

    def first(self, prefix: str) -> ATResult | None:
        matches = self.matching(prefix)
        return matches[0] if matches else None


@dataclass
class _PendingTransaction:
    command: str
    expected_prefixes: frozenset[str]
    require_result: bool
    future: asyncio.Future[ATCommandResult]
    results: list[ATResult] = field(default_factory=list)


class ATCommandBroker:
    """Serialize AT commands and correlate results with their terminal reply.

    During SLC, ``event_queue`` may be passed to :meth:`execute` so the broker
    temporarily pumps the queue itself.  After SLC, exactly one
    :class:`ATEventDispatcher` must own the queue and call :meth:`handle_event`.
    Unrelated URCs are never swallowed; direct-mode transactions put them back
    for the active dispatcher in arrival order.
    """

    def __init__(
        self,
        state: HFPState,
        *,
        default_timeout: float = AT_TIMEOUT_SECONDS,
        terminal_quarantine_seconds: float = AT_TERMINAL_QUARANTINE_SECONDS,
        command_queue: asyncio.Queue | None = None,
    ) -> None:
        self._state = state
        self._default_timeout = default_timeout
        if terminal_quarantine_seconds <= 0:
            raise ValueError("AT terminal quarantine must be positive")
        self._terminal_quarantine_seconds = float(terminal_quarantine_seconds)
        self._command_queue = (
            command_queue if command_queue is not None else state._at_cmd_queue
        )
        self.generation = int(getattr(state, "connection_generation", 0))
        self._command_lock = asyncio.Lock()
        self._pending: _PendingTransaction | None = None
        self._terminal_quarantine_until: float | None = None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def terminal_quarantined(self) -> bool:
        """Whether a timed-out command still owns the next bare terminal."""

        until = self._terminal_quarantine_until
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self._terminal_quarantine_until = None
        log.warning("AT terminal quarantine expired without a late response")
        return False

    async def execute(
        self,
        command: str,
        *,
        expected_prefixes: Iterable[str] = (),
        require_result: bool | None = None,
        timeout: float | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> ATCommandResult:
        encoded = _encode_command(command)
        expected = frozenset(prefix.upper() for prefix in expected_prefixes)
        if require_result is None:
            require_result = bool(expected)
        command_timeout = self._default_timeout if timeout is None else timeout
        if command_timeout <= 0:
            raise ValueError("AT command timeout must be positive")
        if self._command_queue is None:
            raise RuntimeError("AT command queue is not initialized")
        async with self._command_lock:
            if self.terminal_quarantined:
                raise ATCommandError(
                    command,
                    "previous AT command terminal response unresolved",
                )
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ATCommandResult] = loop.create_future()
            pending = _PendingTransaction(
                command=command,
                expected_prefixes=expected,
                require_result=require_result,
                future=future,
            )
            self._pending = pending
            deferred: list[ParsedAT] = []
            deadline = loop.time() + command_timeout
            command_queued = False
            try:
                await self._command_queue.put(encoded)
                command_queued = True
                if event_queue is not None:
                    while not future.done():
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        event = await asyncio.wait_for(event_queue.get(), remaining)
                        if event is None:
                            self.abort("RFCOMM connection closed")
                        elif not self.handle_event(event):
                            deferred.append(event)
                remaining = deadline - loop.time()
                if remaining <= 0 and not future.done():
                    raise asyncio.TimeoutError
                return await asyncio.wait_for(
                    asyncio.shield(future), max(remaining, 0.001)
                )
            except asyncio.TimeoutError as exc:
                # HFP terminal responses carry no transaction identifier. If a
                # late OK were allowed to overlap the next command it could be
                # misattributed (for example ATD's OK satisfying AT+CHUP).
                # Quarantine the channel until the orphan is consumed or this
                # connection-local broker is replaced.
                self._terminal_quarantine_until = (
                    time.monotonic() + self._terminal_quarantine_seconds
                )
                raise ATCommandTimeout(command, "timeout") from exc
            except asyncio.CancelledError:
                # Once bytes have been queued, task cancellation does not
                # cancel the phone's command. Its terminal may still arrive.
                if command_queued and not future.done():
                    self._terminal_quarantine_until = (
                        time.monotonic() + self._terminal_quarantine_seconds
                    )
                raise
            finally:
                if self._pending is pending:
                    self._pending = None
                if not future.done():
                    future.cancel()
                if event_queue is not None:
                    for event in deferred:
                        event_queue.put_nowait(event)

    def handle_event(self, event: ParsedAT) -> bool:
        """Consume an event if it belongs to the current transaction."""

        if isinstance(event, ATResponse) and self._terminal_quarantine_until is not None:
            self._terminal_quarantine_until = None
            log.warning(
                "Consumed quarantined terminal response after AT timeout: %s",
                event,
            )
            return True
        pending = self._pending
        if pending is None:
            return False
        if isinstance(event, ATResult):
            if event.prefix not in pending.expected_prefixes:
                return False
            pending.results.append(event)
            return True
        if not isinstance(event, ATResponse):
            return False

        if pending.future.done():
            return True
        if not event.success:
            pending.future.set_exception(
                ATCommandError(pending.command, event.code or "ERROR")
            )
        elif pending.require_result and not pending.results:
            expected = ", ".join(sorted(pending.expected_prefixes)) or "result"
            pending.future.set_exception(
                ATCommandError(pending.command, f"OK without expected {expected}")
            )
        else:
            pending.future.set_result(
                ATCommandResult(
                    command=pending.command,
                    response=event,
                    results=tuple(pending.results),
                )
            )
        return True

    def abort(self, reason: str = "RFCOMM connection closed") -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(ATConnectionClosed(pending.command, reason))


def _encode_command(command: str) -> bytes:
    if not isinstance(command, str):
        raise TypeError("AT command must be a string")
    if "\n" in command or "\r" in command[:-1] or not command.endswith("\r"):
        raise ValueError("AT command must contain exactly one trailing CR")
    try:
        encoded = command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("AT command must be ASCII") from exc
    if len(encoded) > MAX_AT_COMMAND_BYTES:
        raise ValueError(f"AT command exceeds {MAX_AT_COMMAND_BYTES} bytes")
    return encoded


def get_at_command_broker(state: HFPState) -> ATCommandBroker:
    """Return the connection-local broker attached to a legacy ``HFPState``."""

    broker = getattr(state, "_at_command_broker", None)
    if not isinstance(broker, ATCommandBroker):
        broker = ATCommandBroker(state)
        setattr(state, "_at_command_broker", broker)
    return broker


def reset_at_command_broker(
    state: HFPState,
    *,
    command_queue: asyncio.Queue | None = None,
) -> ATCommandBroker:
    """Abort stale work and create a broker for a new RFCOMM generation."""

    old = getattr(state, "_at_command_broker", None)
    if isinstance(old, ATCommandBroker):
        old.abort("RFCOMM connection replaced")
    broker = ATCommandBroker(state, command_queue=command_queue)
    setattr(state, "_at_command_broker", broker)
    return broker


class RFCOMMThread(threading.Thread):
    """Own one RFCOMM socket and its blocking reader/writer loops."""

    def __init__(
        self,
        state: HFPState,
        sock: socket.socket | None = None,
        *,
        generation: int | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        event_queue: asyncio.Queue | None = None,
        command_queue: asyncio.Queue | None = None,
    ) -> None:
        super().__init__(daemon=True, name=f"rfcomm-io-{generation}")
        self._state = state
        self._sock = sock if sock is not None else state.rfcomm_socket
        if self._sock is None:
            raise RuntimeError("RFCOMM socket is not initialized")
        self.generation = (
            int(getattr(state, "connection_generation", 0))
            if generation is None
            else generation
        )
        self._loop = loop if loop is not None else state._asyncio_loop
        self._event_queue = (
            event_queue if event_queue is not None else state._at_event_queue
        )
        self._command_queue = (
            command_queue if command_queue is not None else state._at_cmd_queue
        )
        if self._loop is None or self._event_queue is None or self._command_queue is None:
            raise RuntimeError("RFCOMM asyncio queues are not initialized")
        self._stop_event = threading.Event()
        self._writer: threading.Thread | None = None
        self._sentinel_posted = False

    def run(self) -> None:
        parser = ATParser()
        self._writer = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"rfcomm-write-{self.generation}",
        )
        self._writer.start()
        try:
            while not self._stop_event.is_set():
                try:
                    data = self._sock.recv(1024)
                except OSError as exc:
                    if exc.errno in {
                        errno.EAGAIN,
                        errno.EWOULDBLOCK,
                    }:
                        # BlueZ may pass Profile1 RFCOMM descriptors with a
                        # kernel receive timeout. HFP control links are often
                        # legitimately silent for minutes; an idle timeout is
                        # not a disconnect (SO_RCVTIMEO yields EAGAIN).
                        # ETIMEDOUT is a lost connection, not an idle timer;
                        # swallowing it leaves a dead socket marked connected.
                        continue
                    if not self._stop_event.is_set():
                        log.error("RFCOMM recv error: %s", exc)
                    break
                if not data:
                    log.info("RFCOMM socket closed by remote")
                    break
                try:
                    events = parser.feed(data)
                except ATProtocolError as exc:
                    log.error("RFCOMM protocol error: %s", exc)
                    break
                for event in events:
                    self._post_event(event)
        finally:
            self._stop_event.set()
            self._close_socket()
            if self._writer is not None and self._writer is not threading.current_thread():
                self._writer.join(timeout=1.0)
            self._post_sentinel_once()

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            future = asyncio.run_coroutine_threadsafe(
                self._command_queue.get(), self._loop
            )
            try:
                cmd_bytes = future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                future.cancel()
                continue
            except (concurrent.futures.CancelledError, RuntimeError):
                break
            if not isinstance(cmd_bytes, bytes):
                log.error("RFCOMM writer rejected non-bytes command")
                continue
            try:
                self._sock.sendall(cmd_bytes)
            except OSError as exc:
                if not self._stop_event.is_set():
                    log.error("RFCOMM send error: %s", exc)
                self._stop_event.set()
                self._close_socket()
                break

    def stop(self) -> None:
        self._stop_event.set()
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _post_sentinel_once(self) -> None:
        if not self._sentinel_posted:
            self._sentinel_posted = True
            self._post_event(None)

    def _post_event(self, event: ParsedAT | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        except RuntimeError:
            # Event loop shutdown already owns final process teardown.
            pass


@dataclass(frozen=True)
class CallInfo:
    """Connection-local call metadata not represented by the legacy state."""

    generation: int = 0
    direction: CallDirection | None = None
    number: str | None = None
    name: str | None = None
    calls: tuple[CLCCEntry, ...] = ()


class ATEventDispatcher:
    """The sole active-session consumer of parsed RFCOMM events."""

    def __init__(
        self,
        state: HFPState,
        on_call_ended: Optional[Callable[[], None]] = None,
        *,
        broker: ATCommandBroker | None = None,
        on_call_info: Optional[Callable[[CallInfo], None]] = None,
        on_call_ended_info: Optional[Callable[[CallInfo], None]] = None,
        generation: int | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        self._state = state
        self._on_call_ended = on_call_ended
        self._on_call_info = on_call_info
        self._on_call_ended_info = on_call_ended_info
        self._broker = broker if broker is not None else get_at_command_broker(state)
        self._event_queue = (
            event_queue if event_queue is not None else state._at_event_queue
        )
        self._generation = (
            int(getattr(state, "connection_generation", 0))
            if generation is None
            else generation
        )
        with state._lock:
            present = state.call_state != CallState.IDLE
        self._call_info = CallInfo(generation=1 if present else 0)

    @property
    def call_info(self) -> CallInfo:
        return self._call_info

    @property
    def broker(self) -> ATCommandBroker:
        return self._broker

    async def run(self) -> None:
        if self._event_queue is None:
            raise RuntimeError("AT event queue is not initialized")
        while True:
            event = await self._event_queue.get()
            if self._is_stale_generation():
                self._broker.abort("RFCOMM connection generation replaced")
                return
            if event is None:
                log.info("RFCOMM socket closed")
                self._broker.abort()
                with self._state._lock:
                    had_call = self._state.call_state != CallState.IDLE
                ended_info = self._call_info
                try:
                    disconnected = self._state.set_disconnected(self._generation)
                except TypeError:
                    disconnected = self._state.set_disconnected()
                if disconnected is not False and had_call:
                    self._notify_call_ended(ended_info)
                return
            self._dispatch(event)

    def _dispatch(self, event: ParsedAT) -> None:
        if self._broker.handle_event(event):
            return
        if isinstance(event, (ATUnsolicited, ATResult)):
            self._handle_urc(event.prefix, event.payload)
        elif isinstance(event, ATResponse):
            log.warning("Ignoring orphan AT terminal response: %s", event)

    def _is_stale_generation(self) -> bool:
        current = getattr(self._state, "connection_generation", self._generation)
        return bool(self._generation and current != self._generation)

    def _handle_urc(self, prefix: str, payload: str) -> None:
        if prefix == URC_CIEV:
            self._handle_ciev(payload)
        elif prefix == "RING":
            self._set_call_state(CallState.INCOMING, CallDirection.INCOMING)
        elif prefix == URC_CLIP:
            try:
                caller = parse_clip(payload)
            except ATProtocolError as exc:
                log.warning("Invalid +CLIP result: %s", exc)
            else:
                self._apply_clip(caller)
        elif prefix == "+CLCC":
            try:
                entry = parse_clcc(payload)
            except ATProtocolError as exc:
                log.warning("Invalid +CLCC result: %s", exc)
            else:
                self.apply_clcc_entries((entry,))
        elif prefix in ("NO CARRIER", "BUSY", "NO ANSWER"):
            log.info("Call ended by remote (%s)", prefix)
            self._end_call()
        elif prefix == "+BCS":
            # Codec negotiation is deliberately not advertised.  Accepting a
            # codec here would make the SDP/BRSF capability contract false.
            log.warning("Unexpected codec negotiation request: %s", payload)

    def _handle_ciev(self, payload: str) -> None:
        parts = payload.split(",")
        if len(parts) != 2:
            return
        try:
            idx, val = (int(part.strip(), 10) for part in parts)
        except ValueError:
            return

        name: str | None = None
        with self._state._lock:
            for indicator_name, indicator in self._state.indicators.items():
                if indicator[0] == idx:
                    indicator[1] = val
                    name = indicator_name
                    break
        if name in {"call", "callsetup", "callheld"}:
            self._reconcile_indicators()

    def _reconcile_indicators(self) -> None:
        with self._state._lock:
            values = {
                name: int(indicator[1])
                for name, indicator in self._state.indicators.items()
            }
        setup = values.get("callsetup", 0)
        active = values.get("call", 0) == 1
        held = values.get("callheld", 0) != 0
        if setup == 1:
            self._set_call_state(CallState.INCOMING, CallDirection.INCOMING)
        elif setup == 2:
            self._set_call_state(CallState.DIALING, CallDirection.OUTGOING)
        elif setup == 3:
            self._set_call_state(CallState.RINGING, CallDirection.OUTGOING)
        elif held and not active:
            held_state = getattr(CallState, "HELD", CallState.ACTIVE)
            self._set_call_state(held_state, self._call_info.direction)
        elif active:
            self._set_call_state(CallState.ACTIVE, self._call_info.direction)
        else:
            self._end_call()

    async def query_current_calls(self, timeout: float | None = None) -> tuple[CLCCEntry, ...]:
        result = await self._broker.execute(
            CMD_CLCC,
            expected_prefixes=("+CLCC",),
            require_result=False,
            timeout=timeout,
        )
        entries = tuple(parse_clcc(item.payload) for item in result.matching("+CLCC"))
        self.apply_clcc_entries(entries)
        return entries

    def apply_clcc_entries(self, entries: Iterable[CLCCEntry]) -> None:
        calls = tuple(entries)
        if not calls:
            self._end_call()
            return
        priority = {
            CLCCStatus.ACTIVE: 0,
            CLCCStatus.INCOMING: 1,
            CLCCStatus.WAITING: 1,
            CLCCStatus.DIALING: 2,
            CLCCStatus.ALERTING: 3,
            CLCCStatus.HELD: 4,
            CLCCStatus.HELD_BY_RESPONSE_AND_HOLD: 4,
        }
        primary = min(calls, key=lambda call: priority[call.status])
        state_by_status = {
            CLCCStatus.ACTIVE: CallState.ACTIVE,
            CLCCStatus.HELD: getattr(CallState, "HELD", CallState.ACTIVE),
            CLCCStatus.DIALING: CallState.DIALING,
            CLCCStatus.ALERTING: CallState.RINGING,
            CLCCStatus.INCOMING: CallState.INCOMING,
            CLCCStatus.WAITING: CallState.INCOMING,
            CLCCStatus.HELD_BY_RESPONSE_AND_HOLD: CallState.ACTIVE,
        }
        self._set_call_state(state_by_status[primary.status], primary.direction)
        self._call_info = CallInfo(
            generation=self._call_info.generation,
            direction=primary.direction,
            number=primary.number or self._call_info.number,
            name=primary.name or self._call_info.name,
            calls=calls,
        )
        set_identity = getattr(self._state, "set_remote_identity", None)
        if callable(set_identity) and primary.number:
            set_identity(
                primary.number,
                # The daemon normalizes this untrusted network caller-ID before
                # it can become an authorization input.
                source="clcc_unvalidated",
                name=primary.name,
                direction=_direction_text(primary.direction),
            )
        self._notify_call_info()

    def _apply_clip(self, caller: CLIPInfo) -> None:
        self._call_info = CallInfo(
            generation=self._call_info.generation,
            direction=CallDirection.INCOMING,
            number=caller.number,
            name=caller.name,
            calls=self._call_info.calls,
        )
        set_identity = getattr(self._state, "set_remote_identity", None)
        if callable(set_identity):
            set_identity(
                caller.number,
                source="clip_unvalidated",
                name=caller.name,
                direction="incoming",
            )
        self._notify_call_info()

    def _set_call_state(
        self,
        state: CallState,
        direction: CallDirection | None,
    ) -> None:
        with self._state._lock:
            if (
                self._state.call_state == CallState.ENDING
                and state != CallState.IDLE
            ):
                return
            was_idle = self._state.call_state == CallState.IDLE
        generation = self._call_info.generation + 1 if was_idle else self._call_info.generation
        direction_text = _direction_text(direction)
        try:
            self._state.set_call_state(state, direction=direction_text)
        except TypeError:
            # Compatibility with the original flat HFPState during rollout.
            self._state.set_call_state(state)
        self._call_info = CallInfo(
            generation=generation,
            direction=direction,
            number=self._call_info.number,
            name=self._call_info.name,
            calls=() if was_idle else self._call_info.calls,
        )
        self._notify_call_info()

    def _end_call(self) -> None:
        with self._state._lock:
            had_call = self._state.call_state != CallState.IDLE
        ended_info = self._call_info
        self._state.set_call_state(CallState.IDLE)
        # ``audio_active`` is transport readiness, never inferred from CIND.
        self._state.set_audio_active(False)
        self._call_info = CallInfo(generation=self._call_info.generation)
        self._notify_call_info()
        if had_call:
            self._notify_call_ended(ended_info)

    def _notify_call_info(self) -> None:
        if self._on_call_info is None:
            return
        try:
            self._on_call_info(self._call_info)
        except Exception as exc:
            log.debug("Call-info callback failed: %s", exc)

    def _notify_call_ended(self, ended_info: CallInfo | None = None) -> None:
        if self._on_call_ended_info is not None:
            try:
                self._on_call_ended_info(ended_info or self._call_info)
            except Exception as exc:
                log.debug("Call-ended metadata callback failed: %s", exc)
        # Resource revocation comes after metadata capture so callbacks can
        # still associate the exact media lease with the call that ended.
        if self._on_call_ended is not None:
            try:
                self._on_call_ended()
            except Exception as exc:
                log.debug("Call-ended cleanup callback failed: %s", exc)


def _direction_text(direction: CallDirection | None) -> str | None:
    if direction == CallDirection.INCOMING:
        return "incoming"
    if direction == CallDirection.OUTGOING:
        return "outgoing"
    return None
