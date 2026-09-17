"""Feature-driven HFP 1.8 service-level connection establishment."""

from __future__ import annotations

import asyncio
import logging

from ..config import AT_TIMEOUT_SECONDS, HFP_HF_BRSF_FEATURES
from ..state import CallState, HFPState
from .protocol import (
    ATProtocolError,
    CMD_BRSF,
    CMD_CHLD_LIST,
    CMD_CIND_LIST,
    CMD_CIND_READ,
    CMD_CLIP_ENABLE,
    CMD_CMER,
    parse_cind_definition,
    parse_cind_values,
)
from .session import (
    ATCommandBroker,
    ATCommandError,
    get_at_command_broker,
)

log = logging.getLogger(__name__)

# AT+BRSF Hands-Free feature bits (HFP 1.8, Table 3.7).  This mask is
# deliberately independent from the SDP SupportedFeatures mask in profile.py.
HF_FEATURE_EC_NR = 1 << 0
HF_FEATURE_THREE_WAY = 1 << 1
HF_FEATURE_CLI_PRESENTATION = 1 << 2
HF_FEATURE_VOICE_RECOGNITION = 1 << 3
HF_FEATURE_REMOTE_VOLUME = 1 << 4
HF_FEATURE_ENHANCED_CALL_STATUS = 1 << 5
HF_FEATURE_ENHANCED_CALL_CONTROL = 1 << 6
HF_FEATURE_CODEC_NEGOTIATION = 1 << 7

# Caller ID and enhanced call status are implemented.  Remote volume, codec
# negotiation, and three-way commands are not advertised until end-to-end
# implementations exist.
HF_BRSF_FEATURES = HFP_HF_BRSF_FEATURES

# AT+BRSF Audio Gateway feature bits use a different mapping.
AG_FEATURE_THREE_WAY = 1 << 0


class HandshakeError(RuntimeError):
    pass


class HFPHandshaker:
    """Run the HF side of HFP SLC establishment.

    The HF always initiates the SLC with ``AT+BRSF``.  An AG may initiate the
    underlying RFCOMM connection, but it does not reverse AT command roles.
    """

    def __init__(
        self,
        state: HFPState,
        timeout: float = AT_TIMEOUT_SECONDS,
        *,
        hf_features: int = HF_BRSF_FEATURES,
        broker: ATCommandBroker | None = None,
        generation: int | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        self._state = state
        self._timeout = timeout
        self._hf_features = hf_features
        self._broker = broker if broker is not None else get_at_command_broker(state)
        self._event_queue = (
            event_queue if event_queue is not None else state._at_event_queue
        )
        self._generation = (
            int(getattr(state, "connection_generation", 0))
            if generation is None
            else generation
        )

    @property
    def broker(self) -> ATCommandBroker:
        return self._broker

    async def run(self) -> None:
        queue = self._event_queue
        if queue is None:
            raise HandshakeError("AT event queue is not initialized")
        log.info("Starting HF-initiated HFP service-level connection")
        try:
            brsf_tx = await self._broker.execute(
                CMD_BRSF.format(features=self._hf_features),
                expected_prefixes=("+BRSF",),
                timeout=self._timeout,
                event_queue=queue,
            )
            brsf = brsf_tx.first("+BRSF")
            if brsf is None:
                raise HandshakeError("Audio gateway omitted +BRSF result")
            remote_features = _parse_feature_mask(brsf.payload, "+BRSF")
            self._ensure_current_generation()
            with self._state._lock:
                self._state.remote_brsf = remote_features

            cind_def_tx = await self._broker.execute(
                CMD_CIND_LIST,
                expected_prefixes=("+CIND",),
                timeout=self._timeout,
                event_queue=queue,
            )
            cind_def = cind_def_tx.first("+CIND")
            assert cind_def is not None
            indicator_map = parse_cind_definition(cind_def.payload)
            missing = {"call", "callsetup"} - indicator_map.keys()
            if missing:
                raise HandshakeError(
                    "Audio gateway omitted mandatory indicators: "
                    + ", ".join(sorted(missing))
                )

            cind_read_tx = await self._broker.execute(
                CMD_CIND_READ,
                expected_prefixes=("+CIND",),
                timeout=self._timeout,
                event_queue=queue,
            )
            cind_read = cind_read_tx.first("+CIND")
            assert cind_read is not None
            values = parse_cind_values(cind_read.payload)
            if len(values) < max(indicator_map.values()):
                raise HandshakeError(
                    "Audio gateway returned fewer +CIND values than definitions"
                )
            self._ensure_current_generation()
            with self._state._lock:
                self._state.indicators = {
                    name: [index, values[index - 1]]
                    for name, index in indicator_map.items()
                }
            _apply_initial_call_state(self._state)

            await self._broker.execute(
                CMD_CMER,
                timeout=self._timeout,
                event_queue=queue,
            )

            if (
                self._hf_features & HF_FEATURE_THREE_WAY
                and remote_features & AG_FEATURE_THREE_WAY
            ):
                await self._broker.execute(
                    CMD_CHLD_LIST,
                    expected_prefixes=("+CHLD",),
                    timeout=self._timeout,
                    event_queue=queue,
                )

            if self._hf_features & HF_FEATURE_CLI_PRESENTATION:
                await self._broker.execute(
                    CMD_CLIP_ENABLE,
                    timeout=self._timeout,
                    event_queue=queue,
                )
        except HandshakeError:
            raise
        except (ATCommandError, ATProtocolError, ValueError) as exc:
            raise HandshakeError(str(exc)) from exc

        self._ensure_current_generation()
        try:
            completed = self._state.set_handshake_complete(self._generation)
        except TypeError:
            completed = self._state.set_handshake_complete()
        if completed is False:
            raise HandshakeError("RFCOMM connection generation was replaced")
        log.info("HFP SLC established with %s", self._state.connected_address)

    def _ensure_current_generation(self) -> None:
        current = getattr(self._state, "connection_generation", self._generation)
        if self._generation and current != self._generation:
            raise HandshakeError("RFCOMM connection generation was replaced")


def _parse_feature_mask(payload: str, label: str) -> int:
    try:
        value = int(payload.strip(), 10)
    except ValueError as exc:
        raise HandshakeError(f"Invalid {label} feature mask: {payload!r}") from exc
    if value < 0:
        raise HandshakeError(f"Invalid {label} feature mask: {payload!r}")
    return value


def _apply_initial_call_state(state: HFPState) -> None:
    """Preserve call state reported by the initial ``AT+CIND?`` response."""

    with state._lock:
        values = {
            name: int(indicator[1])
            for name, indicator in state.indicators.items()
        }
    setup = values.get("callsetup", 0)
    if setup == 1:
        call_state = CallState.INCOMING
    elif setup == 2:
        call_state = CallState.DIALING
    elif setup == 3:
        call_state = CallState.RINGING
    elif values.get("call", 0) == 1 or values.get("callheld", 0) != 0:
        call_state = CallState.ACTIVE
    else:
        call_state = CallState.IDLE
    state.set_call_state(call_state)
    # CIND indicates call control state, not physical SCO readiness.
    state.set_audio_active(False)
