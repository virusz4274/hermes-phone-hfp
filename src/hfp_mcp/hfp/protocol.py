"""AT commands, bounded framing, and typed HFP result parsing.

HFP uses a byte-stream RFCOMM channel.  Audio gateways normally frame result
lines as ``CR LF <content> CR LF``; :class:`ATParser` accepts fragmented and
coalesced input while placing a hard bound on unframed data.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from enum import IntEnum
from io import StringIO
from typing import Union

# Commands sent by the Hands-Free unit (us) to the Audio Gateway (phone).
CMD_BRSF = "AT+BRSF={features}\r"
CMD_CIND_LIST = "AT+CIND=?\r"
CMD_CIND_READ = "AT+CIND?\r"
CMD_CMER = "AT+CMER=3,0,0,1\r"
CMD_CHLD_LIST = "AT+CHLD=?\r"
CMD_CLIP_ENABLE = "AT+CLIP=1\r"
CMD_ATD = "ATD{number};\r"
CMD_ATA = "ATA\r"
CMD_CHUP = "AT+CHUP\r"
CMD_CLCC = "AT+CLCC\r"
CMD_BCC = "AT+BCC\r"
CMD_VGS = "AT+VGS={gain}\r"
CMD_VGM = "AT+VGM={gain}\r"
CMD_BCS_ACCEPT = "AT+BCS={codec}\r"

URC_CIEV = "+CIEV"
URC_CLIP = "+CLIP"
URC_BCS = "+BCS"
URC_BSIR = "+BSIR"
URC_RING = "RING"

DEFAULT_MAX_BUFFER_BYTES = 8192
DEFAULT_MAX_FRAME_BYTES = 4096


class ATProtocolError(ValueError):
    """Raised when an RFCOMM peer sends invalid or unbounded AT framing."""


@dataclass(frozen=True)
class ATResponse:
    """Terminal ``OK``, ``ERROR`` or ``+CME ERROR`` line."""

    success: bool
    code: str = ""


@dataclass(frozen=True)
class ATResult:
    """Intermediate result line such as ``+BRSF: 24``."""

    prefix: str
    payload: str


@dataclass(frozen=True)
class ATUnsolicited:
    """Bare unsolicited result such as ``RING`` or ``NO CARRIER``."""

    prefix: str
    payload: str


ParsedAT = Union[ATResponse, ATResult, ATUnsolicited]


class CallDirection(IntEnum):
    OUTGOING = 0
    INCOMING = 1


class CLCCStatus(IntEnum):
    ACTIVE = 0
    HELD = 1
    DIALING = 2
    ALERTING = 3
    INCOMING = 4
    WAITING = 5
    HELD_BY_RESPONSE_AND_HOLD = 6


@dataclass(frozen=True)
class CLIPInfo:
    number: str
    number_type: int
    subaddress: str | None = None
    subaddress_type: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class CLCCEntry:
    index: int
    direction: CallDirection
    status: CLCCStatus
    mode: int
    multiparty: bool
    number: str | None = None
    number_type: int | None = None
    name: str | None = None


class ATParser:
    """Accumulate RFCOMM bytes and return complete, typed AT lines.

    The parser has one producer (the RFCOMM reader thread).  A peer that sends
    an overlong frame is treated as a protocol failure rather than being
    allowed to grow process memory indefinitely.
    """

    def __init__(
        self,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        if max_buffer_bytes < 4 or max_frame_bytes < 1:
            raise ValueError("AT parser limits must be positive")
        if max_frame_bytes > max_buffer_bytes:
            raise ValueError("max_frame_bytes cannot exceed max_buffer_bytes")
        self._max_buffer_bytes = max_buffer_bytes
        self._max_frame_bytes = max_frame_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[ParsedAT]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("AT parser input must be bytes-like")
        if len(self._buf) + len(data) > self._max_buffer_bytes:
            self.reset()
            raise ATProtocolError(
                f"AT receive buffer exceeded {self._max_buffer_bytes} bytes"
            )
        self._buf.extend(data)
        results: list[ParsedAT] = []

        while True:
            start = self._buf.find(b"\r\n")
            if start < 0:
                break
            if start:
                # Ignore command echo/noise before the first result delimiter,
                # but keep the amount bounded by the limit above.
                del self._buf[:start]
            end = self._buf.find(b"\r\n", 2)
            if end < 0:
                if len(self._buf) - 2 > self._max_frame_bytes:
                    self.reset()
                    raise ATProtocolError(
                        f"AT frame exceeded {self._max_frame_bytes} bytes"
                    )
                break
            frame = bytes(self._buf[2:end])
            del self._buf[: end + 2]
            if len(frame) > self._max_frame_bytes:
                self.reset()
                raise ATProtocolError(
                    f"AT frame exceeded {self._max_frame_bytes} bytes"
                )
            line = frame.decode("ascii", errors="replace").strip()
            if line:
                results.append(_parse_line(line))
        return results

    def reset(self) -> None:
        self._buf.clear()


def _parse_line(line: str) -> ParsedAT:
    upper = line.upper()
    if upper == "OK":
        return ATResponse(success=True)
    if upper == "ERROR":
        return ATResponse(success=False, code="ERROR")
    if upper.startswith("+CME ERROR"):
        return ATResponse(success=False, code=line)
    if ":" in line and line.startswith("+"):
        prefix, _, payload = line.partition(":")
        return ATResult(prefix=prefix.strip().upper(), payload=payload.strip())
    return ATUnsolicited(prefix=upper.strip(), payload="")


def parse_cind_definition(payload: str) -> dict[str, int]:
    """Parse ``+CIND=?`` into ``{indicator_name: one_based_index}``."""

    names = re.findall(r'"([^"]+)"', payload)
    return {name.lower(): idx for idx, name in enumerate(names, start=1)}


def parse_cind_values(payload: str) -> list[int]:
    """Parse ``+CIND?`` values without shifting malformed positions."""

    fields = payload.split(",")
    if not fields or any(not field.strip() for field in fields):
        raise ATProtocolError(f"Invalid +CIND value list: {payload!r}")
    values: list[int] = []
    for field in fields:
        try:
            values.append(int(field.strip(), 10))
        except ValueError as exc:
            raise ATProtocolError(f"Invalid +CIND value: {field!r}") from exc
    return values


def _csv_fields(payload: str) -> list[str]:
    try:
        row = next(csv.reader(StringIO(payload), skipinitialspace=True))
    except (csv.Error, StopIteration) as exc:
        raise ATProtocolError(f"Invalid HFP CSV payload: {payload!r}") from exc
    return [field.strip() for field in row]


def _parse_int(field: str, label: str) -> int:
    try:
        return int(field, 10)
    except ValueError as exc:
        raise ATProtocolError(f"Invalid {label}: {field!r}") from exc


def parse_clip(payload: str) -> CLIPInfo:
    """Parse an unsolicited ``+CLIP`` caller-ID result."""

    fields = _csv_fields(payload)
    if len(fields) < 2:
        raise ATProtocolError(f"Incomplete +CLIP payload: {payload!r}")
    subaddress = fields[2] or None if len(fields) > 2 else None
    subaddress_type = (
        _parse_int(fields[3], "CLIP subaddress type")
        if len(fields) > 3 and fields[3]
        else None
    )
    name = fields[4] or None if len(fields) > 4 else None
    return CLIPInfo(
        number=fields[0],
        number_type=_parse_int(fields[1], "CLIP number type"),
        subaddress=subaddress,
        subaddress_type=subaddress_type,
        name=name,
    )


def parse_clcc(payload: str) -> CLCCEntry:
    """Parse one ``+CLCC`` current-call result."""

    fields = _csv_fields(payload)
    if len(fields) < 5:
        raise ATProtocolError(f"Incomplete +CLCC payload: {payload!r}")
    try:
        direction = CallDirection(_parse_int(fields[1], "CLCC direction"))
        status = CLCCStatus(_parse_int(fields[2], "CLCC status"))
    except ValueError as exc:
        raise ATProtocolError(f"Invalid +CLCC enum in {payload!r}") from exc
    number = fields[5] or None if len(fields) > 5 else None
    number_type = (
        _parse_int(fields[6], "CLCC number type")
        if len(fields) > 6 and fields[6]
        else None
    )
    name = fields[7] or None if len(fields) > 7 else None
    return CLCCEntry(
        index=_parse_int(fields[0], "CLCC index"),
        direction=direction,
        status=status,
        mode=_parse_int(fields[3], "CLCC mode"),
        multiparty=bool(_parse_int(fields[4], "CLCC multiparty flag")),
        number=number,
        number_type=number_type,
        name=name,
    )
