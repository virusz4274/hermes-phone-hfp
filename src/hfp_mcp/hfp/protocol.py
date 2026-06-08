"""
AT command constants and stateful byte-stream parser for HFP.

HFP frames AT messages as: CR LF <content> CR LF
The ATParser accumulates raw bytes from the RFCOMM socket and yields
strongly-typed objects for each complete line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

# ---------------------------------------------------------------------------
# AT commands sent by the Hands-Free unit (us) to the Audio Gateway (phone)
# ---------------------------------------------------------------------------

CMD_BRSF = "AT+BRSF={features}\r"       # Feature exchange — first HF-initiated step
CMD_CIND_LIST = "AT+CIND=?\r"           # Query supported indicators
CMD_CIND_READ = "AT+CIND?\r"            # Read current indicator values
CMD_CMER = "AT+CMER=3,0,0,1\r"         # Enable unsolicited indicator events
CMD_CHLD_LIST = "AT+CHLD=?\r"           # Query call-hold capabilities (required by spec)
CMD_ATD = "ATD{number};\r"             # Dial — semicolon selects voice call mode
CMD_CHUP = "AT+CHUP\r"                 # Hang up / reject
CMD_CLCC = "AT+CLCC\r"                 # List current calls
CMD_BCC = "AT+BCC\r"                   # Request SCO audio link from phone
CMD_VGS = "AT+VGS={gain}\r"            # Set speaker volume (0-15)
CMD_VGM = "AT+VGM={gain}\r"            # Set microphone gain (0-15)
CMD_BCS_ACCEPT = "AT+BCS={codec}\r"    # Accept codec negotiation (mSBC extension)

# Unsolicited result code prefixes we care about
URC_CIEV = "+CIEV"      # Indicator event: +CIEV:<idx>,<val>
URC_BCS = "+BCS"        # Codec negotiation request: +BCS:<codec>
URC_BSIR = "+BSIR"      # In-band ring tone setting
URC_RING = "RING"       # Incoming call ring (not used for outgoing-only, but safe to handle)

# ---------------------------------------------------------------------------
# Parsed event types
# ---------------------------------------------------------------------------

@dataclass
class ATResponse:
    """Terminal OK / ERROR / +CME ERROR line."""
    success: bool
    code: str = ""   # e.g. "+CME ERROR: 11" when success=False


@dataclass
class ATResult:
    """Intermediate result line with a named prefix, e.g. +BRSF:24."""
    prefix: str    # "+BRSF", "+CIND", "+CLCC", …
    payload: str   # everything after the colon, stripped


@dataclass
class ATUnsolicited:
    """
    Unsolicited result code from the phone, e.g. +CIEV:2,1 or RING.
    Also covers intermediate lines that look like URCs during active session.
    """
    prefix: str
    payload: str


ParsedAT = Union[ATResponse, ATResult, ATUnsolicited]

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(rb"\r\n(.*?)\r\n", re.DOTALL)


class ATParser:
    """
    Accumulate raw bytes; yield parsed AT objects for each complete frame.

    Thread-safe for single-producer use (one RFCOMM reader thread).
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[ParsedAT]:
        self._buf += data
        results: list[ParsedAT] = []
        while True:
            m = _FRAME_RE.search(self._buf)
            if not m:
                break
            line = m.group(1).decode("ascii", errors="replace").strip()
            self._buf = self._buf[m.end():]
            if line:
                results.append(_parse_line(line))
        return results

    def reset(self) -> None:
        self._buf = b""


def _parse_line(line: str) -> ParsedAT:
    if line == "OK":
        return ATResponse(success=True)
    if line == "ERROR":
        return ATResponse(success=False, code="ERROR")
    if line.upper().startswith("+CME ERROR:"):
        return ATResponse(success=False, code=line)
    if line.upper().startswith("+CME ERROR"):
        return ATResponse(success=False, code=line)
    if ":" in line and line.startswith("+"):
        prefix, _, payload = line.partition(":")
        return ATResult(prefix=prefix.strip().upper(), payload=payload.strip())
    # Bare words: RING, NO CARRIER, BUSY, etc.
    return ATUnsolicited(prefix=line.strip().upper(), payload="")


# ---------------------------------------------------------------------------
# CIND helpers
# ---------------------------------------------------------------------------

def parse_cind_definition(payload: str) -> dict[str, int]:
    """
    Parse +CIND=? response into {indicator_name: 1-based_index}.

    Example payload:
      ("call",(0,1)),("callsetup",(0-3)),("service",(0,1)),("signal",(0-5))
    """
    names = re.findall(r'"([^"]+)"', payload)
    return {name: idx for idx, name in enumerate(names, start=1)}


def parse_cind_values(payload: str) -> list[int]:
    """
    Parse +CIND? response into a list of integer values (1-based index = pos+1).

    Example payload: 0,0,1,5,0,0,0
    """
    return [int(v.strip()) for v in payload.split(",") if v.strip().lstrip("-").isdigit()]
