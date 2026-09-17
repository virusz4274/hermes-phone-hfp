"""Focused tests for call-scoped SCO transport metric retention."""

from __future__ import annotations

import threading

import pytest

from hfp_mcp import server
from hfp_mcp.media import MediaLease, MediaLeaseManager
from hfp_mcp.state import HFPState


class MetricSession:
    def __init__(self, **metrics):
        self.metrics = dict(metrics)

    def media_metrics(self):
        return dict(self.metrics)

    def stop(self):
        queued = int(self.metrics.get("playback_queue_bytes", 0))
        self.metrics["playback_queue_bytes"] = 0
        self.metrics["playback_queue_ms"] = 0.0
        self.metrics["playback_teardown_discarded_bytes"] = (
            int(self.metrics.get("playback_teardown_discarded_bytes", 0))
            + queued
        )


class MetricAudioManager:
    def __init__(self, sessions=None):
        self.sessions = dict(sessions or {})

    def get_session(self, stream_id):
        return self.sessions.get(stream_id)

    def remove_session(self, stream_id):
        session = self.sessions.pop(stream_id, None)
        if session is None:
            return False
        session.stop()
        return True

    def has_active_transport(self):
        return False


@pytest.fixture(autouse=True)
def isolated_metric_archive(monkeypatch):
    monkeypatch.setattr(server, "_call_media_metric_archive", {})
    monkeypatch.setattr(
        server,
        "_call_media_metric_archive_lock",
        threading.Lock(),
    )
    monkeypatch.setattr(server, "_last_call_media_metrics", None)
    monkeypatch.setattr(server, "_last_ended_call_id", None)


def lease(call_id: str, stream_id: str, generation: int = 1) -> MediaLease:
    return MediaLease(call_id, "gemini_live", stream_id, generation)


def test_archive_replaces_same_stream_and_aggregates_replacement_streams():
    first = lease("call-one", "stream-one")
    first_session = MetricSession(
        playback_accepted_bytes=320,
        playback_consumed_bytes=160,
        playback_queue_peak_ms=160.0,
    )

    server._archive_media_metrics(first, first_session)
    first_session.metrics["playback_accepted_bytes"] = 640
    server._archive_media_metrics(first, first_session)

    same_stream = server._archived_media_metrics("call-one")
    assert same_stream["playback_accepted_bytes"] == 640

    second = lease("call-one", "stream-two", generation=2)
    server._archive_media_metrics(
        second,
        MetricSession(
            playback_accepted_bytes=160,
            playback_consumed_bytes=160,
            playback_queue_peak_ms=80.0,
        ),
    )

    combined = server._archived_media_metrics("call-one")
    assert combined["playback_accepted_bytes"] == 800
    assert combined["playback_consumed_bytes"] == 320
    assert combined["playback_queue_peak_ms"] == 160.0
    assert combined["stream_count"] == 2
    assert combined["stream_ids"] == ["stream-one", "stream-two"]


def test_archive_retention_is_bounded_without_reordering_updated_calls(monkeypatch):
    monkeypatch.setattr(server, "CALL_MEDIA_METRIC_ARCHIVE_MAX_CALLS", 2)
    one = lease("call-one", "stream-one")
    two = lease("call-two", "stream-two")
    three = lease("call-three", "stream-three")

    server._archive_media_metrics(one, MetricSession(playback_accepted_bytes=1))
    server._archive_media_metrics(two, MetricSession(playback_accepted_bytes=2))
    # A final post-stop update for call one must not make it newer than call two.
    server._archive_media_metrics(one, MetricSession(playback_accepted_bytes=10))
    server._archive_media_metrics(three, MetricSession(playback_accepted_bytes=3))

    assert server._archived_media_metrics("call-one") is None
    assert server._archived_media_metrics("call-two")["playback_accepted_bytes"] == 2
    assert server._archived_media_metrics("call-three")["playback_accepted_bytes"] == 3


def test_detach_archives_before_the_session_can_be_reclaimed(monkeypatch):
    leases = MediaLeaseManager()
    active, _ = leases.acquire("call-reclaimed", "gemini_live")
    session = MetricSession(
        playback_accepted_bytes=960,
        playback_consumed_bytes=640,
        playback_queue_bytes=320,
    )
    audio = MetricAudioManager({active.stream_id: session})
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_manager", audio)
    monkeypatch.setattr(server, "_audio_stream_server", None)
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "_legacy_stream_aliases", {})

    assert server._detach_logical_media_lease(active) is True
    audio.remove_session(active.stream_id)

    retained = server._archived_media_metrics("call-reclaimed")
    assert retained["playback_accepted_bytes"] == 960
    assert retained["playback_queue_bytes"] == 320


async def test_release_updates_archive_after_session_stop(monkeypatch):
    leases = MediaLeaseManager()
    active, _ = leases.acquire("call-final", "gemini_live")
    session = MetricSession(
        playback_accepted_bytes=320,
        playback_consumed_bytes=0,
        playback_queue_bytes=320,
        playback_queue_ms=20.0,
        playback_teardown_discarded_bytes=0,
    )
    audio = MetricAudioManager({active.stream_id: session})
    monkeypatch.setattr(server, "_state", HFPState())
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_manager", audio)
    monkeypatch.setattr(server, "_audio_stream_server", None)
    monkeypatch.setattr(server, "_audio_lifecycle_locks", {})
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "_legacy_stream_aliases", {})

    assert await server._release_media_lease_exact(active) is True

    retained = server._archived_media_metrics("call-final")
    assert retained["playback_queue_bytes"] == 0
    assert retained["playback_teardown_discarded_bytes"] == 320
    assert retained["playback_accepted_bytes"] == 320


async def test_status_and_summary_return_metrics_for_reclaimed_call(monkeypatch):
    finished = lease("call-status", "stream-status")
    server._archive_media_metrics(
        finished,
        MetricSession(
            playback_accepted_bytes=1600,
            playback_consumed_bytes=1600,
            playback_underrun_events=2,
        ),
    )
    monkeypatch.setattr(server, "_media_leases", MediaLeaseManager())

    class LiveManager:
        def status(self, _context=None):
            return {"ok": True, "session_id": "call-status", "state": "stopped"}

        def get_last_call_summary(self, _session_id=None):
            return {"ok": True, "session_id": "call-status"}

    manager = LiveManager()
    monkeypatch.setattr(server, "_get_live_ai_manager", lambda: manager)
    monkeypatch.setattr(server, "_gemini_live_manager", manager)
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)
    monkeypatch.setattr(server, "_runtime_config", None)
    monkeypatch.setattr(server, "_request_ledger", None)

    status = await server.get_live_ai_status()
    summary = server.get_last_call_summary("call-status")

    assert status["transport_metrics"]["playback_accepted_bytes"] == 1600
    assert status["transport_metrics"]["playback_underrun_events"] == 2
    assert summary["transport_metrics"] == status["transport_metrics"]


def test_persisted_summary_keeps_contract_and_adds_archived_metrics(monkeypatch):
    finished = lease("call-persisted", "stream-persisted")
    server._archive_media_metrics(
        finished,
        MetricSession(playback_accepted_bytes=800),
    )
    monkeypatch.setattr(server, "_media_leases", MediaLeaseManager())

    class Ledger:
        def get_call_summary(self, call_id):
            assert call_id == "call-persisted"
            return {"call_id": call_id, "summary": "Call completed."}

    monkeypatch.setattr(server, "_request_ledger", Ledger())

    result = server.get_last_call_summary("call-persisted")

    assert result["ok"] is True
    assert result["result"]["summary"] == "Call completed."
    assert result["transport_metrics"]["playback_accepted_bytes"] == 800


async def test_delayed_old_cleanup_cannot_replace_new_call_status(monkeypatch):
    old = lease("call-old", "stream-old")
    new = lease("call-new", "stream-new")
    server._archive_media_metrics(old, MetricSession(playback_accepted_bytes=100))
    monkeypatch.setattr(server, "_last_ended_call_id", "call-new")
    server._archive_media_metrics(new, MetricSession(playback_accepted_bytes=200))
    # Simulate the old executor completing after the newer call ended.
    server._archive_media_metrics(old, MetricSession(playback_accepted_bytes=150))
    monkeypatch.setattr(server, "_media_leases", MediaLeaseManager())

    class LiveManager:
        def status(self, _context=None):
            return {"ok": True, "session_id": "call-new", "state": "stopped"}

    monkeypatch.setattr(server, "_get_live_ai_manager", lambda: LiveManager())
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)

    status = await server.get_live_ai_status()

    assert status["transport_metrics"]["call_id"] == "call-new"
    assert status["transport_metrics"]["playback_accepted_bytes"] == 200
    assert server._last_call_media_metrics["call_id"] == "call-new"
