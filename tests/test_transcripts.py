import time
import json
from types import SimpleNamespace

import httpx
import pytest

from hfp_mcp.contracts import RequestLedger
from hfp_mcp.live_ai import LiveAIManager
from hfp_mcp import server
from hfp_mcp.settings import RuntimeConfig
from hfp_mcp import phone_cli
from hfp_mcp.gemini_live import GeminiLiveManager


def test_gemini_transcription_keeps_both_speakers_and_flushes_last_words(tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db")
    live = GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None,
                             full_transcripts_enabled=True, transcript_sink=ledger.append_transcript)
    live._session_id = "call-a"
    try:
        live._handle_transcriptions(SimpleNamespace(interim_input_transcription=SimpleNamespace(text="wrong guess")))
        live._handle_transcriptions(SimpleNamespace(input_transcription=SimpleNamespace(text="Hello", finished=True)))
        live._handle_transcriptions(SimpleNamespace(output_transcription=SimpleNamespace(text="How can I help?", finished=True)))
        live._handle_transcriptions(SimpleNamespace(input_transcription=SimpleNamespace(text="Goodbye")))
        live.flush_transcript_fragments()
        assert [(e["direction"], e["text"]) for e in ledger.transcript()["events"]] == [
            ("input", "Hello"), ("output", "How can I help?"), ("input", "Goodbye"),
        ]
    finally:
        ledger.close()


def test_cli_exports_all_pages_pins_call_and_protects_output(monkeypatch, tmp_path):
    paths = []

    def control(path):
        paths.append(path)
        if len(paths) == 1:
            return {"ok": True, "session_id": "call-a", "events": [{"text": "Hello"}],
                    "has_more": True, "next_after_id": 1}
        return {"ok": True, "session_id": "call-a", "events": [{"text": "ഉത്തരം"}],
                "has_more": False, "next_after_id": 2}

    monkeypatch.setattr(phone_cli, "control", control)
    output = tmp_path / "call.json"
    assert phone_cli.main(["transcript", "show", "--output", str(output)]) == 0
    assert paths[1].endswith("call_id=call-a&after_id=1")
    assert [e["text"] for e in json.loads(output.read_text())["events"]] == ["Hello", "ഉത്തരം"]
    assert output.stat().st_mode & 0o077 == 0
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        phone_cli.main(["transcript", "show", "--output", str(output)])
    assert output.read_bytes() == original


def manager(ledger, enabled=True):
    result = LiveAIManager(
        provider="test", model=lambda: "test", availability=lambda: {},
        ensure_stream=None, clear_playback=None, hangup=None,
        full_transcripts_enabled=enabled, transcript_sink=ledger.append_transcript,
        transcript_event_limit=8,
    )
    result._session_id = "call-a"
    return result


def test_complete_transcript_survives_restart_memory_eviction_and_pagination(tmp_path):
    path = tmp_path / "calls.db"
    ledger = RequestLedger(path)
    live = manager(ledger)
    texts = [f"ഉത്തരം {i}" for i in range(12)]
    for text in texts:
        live.add_transcript("input", text)
    long_text = "abcdef" * 1000
    live.append_transcript_fragment("output", long_text[:4500])
    live.append_transcript_fragment("output", long_text, final=True)
    assert len(live.get_call_transcript()["events"]) == 8
    ledger.close()
    reopened = RequestLedger(path)
    try:
        assert reopened.transcript_calls()[0]["events"] == 13
        assert path.stat().st_mode & 0o077 == 0
        page = reopened.transcript(limit=3)
        events = list(page["events"])
        while page["has_more"]:
            page = reopened.transcript("call-a", after_id=page["next_after_id"], limit=3)
            events.extend(page["events"])
        assert [e["text"] for e in events] == texts + [long_text]
    finally:
        reopened.close()


def test_transcript_opt_out_never_persists_text_and_retention_is_enforced(tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db", retention_days=1)
    try:
        manager(ledger, enabled=False).add_transcript("input", "private words")
        assert ledger.transcript_calls() == []
        ledger.append_transcript({"session_id": "old", "provider": "test", "direction": "input",
                                  "text": "expired", "timestamp": time.time() - 172800})
        assert ledger.transcript()["events"] == []
    finally:
        ledger.close()


def test_storage_failure_visible_without_stopping_voice(tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db")
    live = manager(ledger)
    ledger.close()
    live.add_transcript("input", "Still available in memory")
    assert live.status()["transcript_storage_error"] == "ProgrammingError"
    assert live.get_call_transcript()["events"][0]["text"] == "Still available in memory"


@pytest.mark.asyncio
async def test_transcript_http_requires_auth_and_explicit_privacy_opt_in(monkeypatch, tmp_path):
    ledger = RequestLedger(tmp_path / "calls.db")
    manager(ledger).add_transcript("input", "test call")
    config = RuntimeConfig(full_transcripts=True)
    monkeypatch.setattr(server, "_runtime_config", config)
    monkeypatch.setattr(server, "_request_ledger", ledger)
    monkeypatch.setattr(server, "_state", SimpleNamespace(call_id=None))
    app = server.create_http_app(config, "s" * 40, start_bluetooth=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            url = "/v1/phone/transcripts"
            assert (await client.get(url)).status_code == 401
            headers = {"Authorization": "Bearer " + "s" * 40}
            result = await client.get(url, headers=headers)
            assert result.status_code == 200
            assert result.json()["events"][0]["text"] == "test call"
            assert (await client.get(url + "?limit=invalid", headers=headers)).status_code == 400
            monkeypatch.setattr(server, "_runtime_config", RuntimeConfig(full_transcripts=False))
            assert (await client.get(url, headers=headers)).status_code == 403
            assert (await client.get(url + "/calls", headers=headers)).status_code == 403
    finally:
        ledger.close()
