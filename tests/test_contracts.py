import stat

import pytest

from hfp_mcp.contracts import (
    ContractError,
    RequestLedger,
    normalize_phone_number,
    validate_mac,
    validate_request_id,
)
from hfp_mcp.media import MediaLeaseManager


def test_strict_identifiers_and_phone_number_reject_at_injection():
    assert validate_mac("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    assert validate_request_id("call:123.retry-1") == "call:123.retry-1"
    assert normalize_phone_number("+14155552671") == "+14155552671"
    with pytest.raises(ContractError):
        normalize_phone_number("+14155552671\rAT+CHUP")
    with pytest.raises(ContractError):
        validate_mac("not-a-mac")


def test_request_ledger_is_durable_and_rejects_cross_operation_reuse(tmp_path):
    path = tmp_path / "state" / "calls.db"
    ledger = RequestLedger(path)
    response = {"ok": True, "result": {"call_id": "c1"}}
    ledger.store("request-1", "place_call", response)
    assert ledger.get("request-1", "place_call").response == response
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ContractError) as exc:
        ledger.get("request-1", "end_call")
    assert exc.value.code == "request_id_conflict"
    ledger.save_call_summary("c1", "Caller requested a callback.", caller_number="+14155552671")
    assert ledger.get_call_summary("c1")["summary"] == "Caller requested a callback."
    ledger.close()

    reopened = RequestLedger(path)
    assert reopened.get("request-1", "place_call").response == response
    reopened.close()


def test_media_lease_is_exclusive_and_generation_bound():
    leases = MediaLeaseManager()
    first, reused = leases.acquire("call-1", "gemini_live")
    assert reused is False
    same, reused = leases.acquire("call-1", "gemini_live")
    assert reused is True
    assert same == first
    with pytest.raises(ContractError) as exc:
        leases.acquire("call-1", "hermes_classic")
    assert exc.value.code == "audio_owner_conflict"
    assert leases.release(first.stream_id) == first
    second, _ = leases.acquire("call-2", "hermes_classic")
    assert second.generation > first.generation
