"""Tests for the AT command parser and CIND helpers."""

import pytest
from hfp_mcp.hfp.protocol import (
    ATParser,
    ATResponse,
    ATResult,
    ATUnsolicited,
    CMD_ATA,
    parse_cind_definition,
    parse_cind_values,
)


# ---------------------------------------------------------------------------
# ATParser
# ---------------------------------------------------------------------------

def _frame(line: str) -> bytes:
    return f"\r\n{line}\r\n".encode()


def test_parse_ok():
    p = ATParser()
    events = p.feed(_frame("OK"))
    assert len(events) == 1
    assert isinstance(events[0], ATResponse)
    assert events[0].success is True


def test_parse_error():
    p = ATParser()
    events = p.feed(_frame("ERROR"))
    assert isinstance(events[0], ATResponse)
    assert events[0].success is False


def test_parse_cme_error():
    p = ATParser()
    events = p.feed(_frame("+CME ERROR: 11"))
    assert isinstance(events[0], ATResponse)
    assert events[0].success is False
    assert "11" in events[0].code


def test_parse_brsf():
    p = ATParser()
    events = p.feed(_frame("+BRSF:36"))
    assert isinstance(events[0], ATResult)
    assert events[0].prefix == "+BRSF"
    assert events[0].payload == "36"


def test_parse_ciev():
    p = ATParser()
    events = p.feed(_frame("+CIEV:2,1"))
    assert isinstance(events[0], ATResult)
    assert events[0].prefix == "+CIEV"
    assert events[0].payload == "2,1"


def test_parse_ring():
    p = ATParser()
    events = p.feed(_frame("RING"))
    assert isinstance(events[0], ATUnsolicited)
    assert events[0].prefix == "RING"


def test_answer_command_is_ata():
    assert CMD_ATA == "ATA\r"


def test_multiple_frames_in_one_feed():
    p = ATParser()
    data = _frame("+BRSF:36") + _frame("OK")
    events = p.feed(data)
    assert len(events) == 2
    assert isinstance(events[0], ATResult)
    assert isinstance(events[1], ATResponse)


def test_fragmented_feed():
    p = ATParser()
    data = _frame("+BRSF:36")
    # Split at arbitrary byte boundary
    events = p.feed(data[:5])
    assert events == []
    events = p.feed(data[5:])
    assert len(events) == 1
    assert isinstance(events[0], ATResult)


def test_empty_lines_skipped():
    p = ATParser()
    events = p.feed(b"\r\n\r\n\r\nOK\r\n")
    assert len(events) == 1
    assert isinstance(events[0], ATResponse)


# ---------------------------------------------------------------------------
# CIND helpers
# ---------------------------------------------------------------------------

CIND_DEF = '("call",(0,1)),("callsetup",(0-3)),("service",(0,1)),("signal",(0-5))'
CIND_VALS = "0,0,1,4"


def test_parse_cind_definition():
    result = parse_cind_definition(CIND_DEF)
    assert result == {"call": 1, "callsetup": 2, "service": 3, "signal": 4}


def test_parse_cind_values():
    result = parse_cind_values(CIND_VALS)
    assert result == [0, 0, 1, 4]


def test_parse_cind_values_partial():
    result = parse_cind_values("1,0")
    assert result == [1, 0]
