"""
HFP 1.8 connection handshake.

Two roles are supported:
  HF-initiated  — we send AT+BRSF first (standard)
  AG-initiated  — phone sends AT+BRSF first (common on Samsung Android)

After the handshake completes the state is updated to CONNECTED and the
indicator map is populated.  Raises HandshakeError on timeout or protocol
failure.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import AT_TIMEOUT_SECONDS, HFP_HF_FEATURES
from ..state import ConnectionState, HFPState
from .protocol import (
    ATResponse,
    ATResult,
    ATUnsolicited,
    CMD_BRSF,
    CMD_CHLD_LIST,
    CMD_CIND_LIST,
    CMD_CIND_READ,
    CMD_CMER,
    parse_cind_definition,
    parse_cind_values,
)

log = logging.getLogger(__name__)


class HandshakeError(Exception):
    pass


class HFPHandshaker:
    """Run the HFP 1.8 SLC (Service Level Connection) establishment."""

    def __init__(self, state: HFPState, timeout: float = AT_TIMEOUT_SECONDS) -> None:
        self._state = state
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Perform the handshake.

        Brief peek (100 ms) at the incoming queue:
        - If the phone sent AT+BRSF=N first (Samsung AG-initiated quirk),
          handle as AG-initiated.
        - Otherwise (queue empty or non-AG event), do HF-initiated (normal).
        """
        try:
            first = await asyncio.wait_for(
                self._state._at_event_queue.get(), timeout=0.1
            )
        except asyncio.TimeoutError:
            first = None

        if (
            first is not None
            and isinstance(first, ATUnsolicited)
            and first.prefix.startswith("AT+BRSF")
        ):
            # AG-initiated: phone sent AT+BRSF=N before us
            log.info("AG-initiated handshake detected (phone sent AT+BRSF first)")
            features_str = first.prefix.split("=", 1)[-1] if "=" in first.prefix else "0"
            await self._ag_initiated(features_str)
        else:
            # HF-initiated (standard path)
            log.info("HF-initiated handshake")
            if first is not None:
                # Unexpected early event — re-queue for the event dispatcher
                await self._state._at_event_queue.put(first)
            await self._do_hf_initiated()

        self._state.set_handshake_complete()
        log.info("HFP SLC established with %s", self._state.connected_address)

    # ------------------------------------------------------------------
    # HF-initiated path (standard)
    # ------------------------------------------------------------------

    async def _do_hf_initiated(self) -> None:
        brsf = await self._send_and_get(
            CMD_BRSF.format(features=HFP_HF_FEATURES), "+BRSF"
        )
        await self._finish_handshake(brsf)

    # ------------------------------------------------------------------
    # AG-initiated path (phone sent AT+BRSF first — Samsung quirk)
    # ------------------------------------------------------------------

    async def _ag_initiated(self, features_str: str) -> None:
        # Send our feature response directly (not via the AT cmd queue —
        # this is a raw reply to an unsolicited AG command)
        await self._send_raw(
            f"+BRSF:{HFP_HF_FEATURES}\r\nOK\r\n".encode()
        )
        remote = int(features_str) if features_str.strip().lstrip("-").isdigit() else 0
        self._state.remote_brsf = remote
        await self._finish_handshake_from_cind()

    # ------------------------------------------------------------------
    # Shared handshake tail
    # ------------------------------------------------------------------

    async def _finish_handshake(self, brsf_result: ATResult) -> None:
        remote = int(brsf_result.payload) if brsf_result.payload.strip().lstrip("-").isdigit() else 0
        self._state.remote_brsf = remote
        log.debug("Remote BRSF features: 0x%04x", remote)
        await self._finish_handshake_from_cind()

    async def _finish_handshake_from_cind(self) -> None:
        # Step 2: indicator definitions
        cind_def = await self._send_and_get(CMD_CIND_LIST, "+CIND")
        indicator_map = parse_cind_definition(cind_def.payload)
        log.debug("Indicators: %s", indicator_map)

        # Step 3: current indicator values
        cind_vals = await self._send_and_get(CMD_CIND_READ, "+CIND")
        values = parse_cind_values(cind_vals.payload)

        with self._state._lock:
            self._state.indicators = {
                name: [idx, values[idx - 1] if idx <= len(values) else 0]
                for name, idx in indicator_map.items()
            }

        # Step 4: enable unsolicited indicator events
        await self._send_and_wait_ok(CMD_CMER)

        # Step 5: call hold capabilities (required by HFP 1.8 spec)
        await self._send_and_get(CMD_CHLD_LIST, "+CHLD")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _send_and_get(self, cmd: str, expected_prefix: str) -> ATResult:
        """Send a command, collect the matching result line, then wait for OK."""
        await self._send(cmd)
        result: ATResult | None = None
        deadline = asyncio.get_event_loop().time() + self._timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise HandshakeError(
                    f"Timeout waiting for {expected_prefix} (cmd={cmd.strip()!r})"
                )
            try:
                event = await asyncio.wait_for(
                    self._state._at_event_queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise HandshakeError(
                    f"Timeout waiting for {expected_prefix} (cmd={cmd.strip()!r})"
                )
            if isinstance(event, ATResult) and event.prefix == expected_prefix:
                result = event
            elif isinstance(event, ATResponse):
                if not event.success:
                    raise HandshakeError(
                        f"Error response to {cmd.strip()!r}: {event.code}"
                    )
                if result is not None:
                    return result
                raise HandshakeError(
                    f"Got OK before {expected_prefix} for cmd {cmd.strip()!r}"
                )
            # Ignore other events during handshake (e.g. spurious URCs)

    async def _send_and_wait_ok(self, cmd: str) -> None:
        await self._send(cmd)
        deadline = asyncio.get_event_loop().time() + self._timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise HandshakeError(f"Timeout waiting for OK (cmd={cmd.strip()!r})")
            try:
                event = await asyncio.wait_for(
                    self._state._at_event_queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise HandshakeError(f"Timeout waiting for OK (cmd={cmd.strip()!r})")
            if isinstance(event, ATResponse):
                if not event.success:
                    raise HandshakeError(
                        f"Error response to {cmd.strip()!r}: {event.code}"
                    )
                return

    async def _send(self, cmd: str) -> None:
        await self._state._at_cmd_queue.put(cmd.encode("ascii"))

    async def _send_raw(self, data: bytes) -> None:
        await self._state._at_cmd_queue.put(data)
