"""
Active HFP session after the handshake is complete.

Two concurrent entities:

  RFCOMMThread  — daemon OS thread; blocking socket.recv() for incoming AT
                  data from the phone; blocking socket.sendall() for outgoing
                  commands drawn from the asyncio command queue.

  ATEventDispatcher — asyncio Task; consumes AT events from the event queue
                      and drives CallState / audio_active transitions based on
                      +CIEV indicator changes.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading

from ..state import CallState, HFPState
from .protocol import ATParser, ATResponse, ATResult, ATUnsolicited, URC_CIEV

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RFCOMM I/O thread
# ---------------------------------------------------------------------------

class RFCOMMThread(threading.Thread):
    """
    Daemon thread that owns all blocking I/O on the RFCOMM socket.

    Reader loop: recv → ATParser.feed → state.post_at_event
    Writer loop: blocks on asyncio queue (via run_coroutine_threadsafe),
                 then socket.sendall
    """

    def __init__(self, state: HFPState) -> None:
        super().__init__(daemon=True, name="rfcomm-io")
        self._state = state
        self._stop = threading.Event()

    # ------------------------------------------------------------------

    def run(self) -> None:
        sock = self._state.rfcomm_socket
        parser = ATParser()

        writer = threading.Thread(target=self._writer_loop, daemon=True, name="rfcomm-write")
        writer.start()

        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(1024)
                except OSError as exc:
                    if not self._stop.is_set():
                        log.error("RFCOMM recv error: %s", exc)
                    break
                if not data:
                    log.info("RFCOMM socket closed by remote")
                    break
                log.debug("RFCOMM rx %d bytes: %r", len(data), data)
                for event in parser.feed(data):
                    log.debug("AT event: %s", event)
                    self._state.post_at_event(event)
        finally:
            self._stop.set()
            self._state.post_at_event(None)   # sentinel: socket gone

    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        loop = self._state._asyncio_loop
        queue = self._state._at_cmd_queue
        sock = self._state.rfcomm_socket

        while not self._stop.is_set():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(queue.get(), timeout=0.5),
                    loop,
                )
                try:
                    cmd_bytes: bytes = future.result(timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    continue
                log.debug("RFCOMM tx: %r", cmd_bytes)
                sock.sendall(cmd_bytes)
            except OSError as exc:
                log.error("RFCOMM send error: %s", exc)
                break

    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        sock = self._state.rfcomm_socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Asyncio event dispatcher
# ---------------------------------------------------------------------------

class ATEventDispatcher:
    """
    Asyncio Task: reads from _at_event_queue and updates HFPState.

    Maps +CIEV indicator changes to CallState / audio_active transitions.
    A None sentinel means the socket closed; we call set_disconnected and stop.
    """

    def __init__(self, state: HFPState) -> None:
        self._state = state

    async def run(self) -> None:
        while True:
            event = await self._state._at_event_queue.get()
            if event is None:
                log.info("RFCOMM socket sentinel received — marking disconnected")
                self._state.set_disconnected()
                return
            self._dispatch(event)

    # ------------------------------------------------------------------

    def _dispatch(self, event) -> None:
        if isinstance(event, ATUnsolicited):
            self._handle_urc(event.prefix, event.payload)
        elif isinstance(event, ATResult):
            self._handle_urc(event.prefix, event.payload)
        # ATResponse during active session = reply to a command we sent;
        # the tool coroutine that sent the command is responsible for reading it.

    def _handle_urc(self, prefix: str, payload: str) -> None:
        if prefix == URC_CIEV:
            self._handle_ciev(payload)
        elif prefix in ("NO CARRIER", "BUSY", "NO ANSWER"):
            log.info("Call ended by remote (%s)", prefix)
            self._state.set_call_state(CallState.IDLE)
            self._state.set_audio_active(False)
        elif prefix == "+BCS":
            # Codec negotiation — accept CVSD (1) only; ignore mSBC for now
            log.debug("+BCS codec negotiation: %s (ignored, using CVSD)", payload)

    def _handle_ciev(self, payload: str) -> None:
        """
        +CIEV:<indicator_index>,<value>

        Key indicators:
          call      0=no call active,  1=call active
          callsetup 0=idle, 1=incoming, 2=outgoing dialing, 3=outgoing ringing
        """
        parts = payload.split(",")
        if len(parts) != 2:
            return
        try:
            idx, val = int(parts[0]), int(parts[1])
        except ValueError:
            return

        # Update stored value
        name: str | None = None
        with self._state._lock:
            for n, (i, _) in self._state.indicators.items():
                if i == idx:
                    self._state.indicators[n][1] = val
                    name = n
                    break

        if name is None:
            return

        log.debug("CIEV %s=%d", name, val)

        if name == "call":
            if val == 1:
                self._state.set_call_state(CallState.ACTIVE)
                self._state.set_audio_active(True)
                log.info("Call ACTIVE — audio link up")
            else:
                self._state.set_call_state(CallState.IDLE)
                self._state.set_audio_active(False)
                log.info("Call ended")
        elif name == "callsetup":
            current = self._state.call_state
            if val == 2 and current == CallState.IDLE:
                self._state.set_call_state(CallState.DIALING)
            elif val == 3:
                self._state.set_call_state(CallState.RINGING)
            elif val == 0 and current in (CallState.DIALING, CallState.RINGING):
                # callsetup cleared without call becoming active → call failed/rejected
                self._state.set_call_state(CallState.IDLE)
