import asyncio
import struct
import threading

import pytest

from hfp_mcp import gemini_live
from hfp_mcp.audio.startup_probe import StartupAudioProbe


async def test_cold_availability_check_does_not_block_call_control():
    manager = gemini_live.GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None)
    main_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def cold_check():
        assert threading.get_ident() != main_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), 'call control was blocked by the availability check'
        return {'available': True}

    manager._availability = cold_check
    check = asyncio.create_task(manager._check_availability())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # An answer/hangup/renewal coroutine can run while imports are in flight.
        await asyncio.sleep(0)
        assert not check.done()
    finally:
        release.set()
    assert (await check)['available']


@pytest.mark.parametrize('available', [True, False])
async def test_warmup_prepares_local_dependencies_off_thread(monkeypatch, available):
    calls = []
    main_thread = threading.get_ident()

    def check(**kwargs):
        assert kwargs == {'configured': True}
        assert threading.get_ident() != main_thread
        calls.append('sdk')
        return {'available': available}

    def audio():
        assert threading.get_ident() != main_thread
        calls.append('audio')

    monkeypatch.setattr(gemini_live, 'availability', check)
    monkeypatch.setattr(gemini_live, '_warm_audio_dependencies', audio)
    assert (await gemini_live.warmup())['available'] is available
    assert calls == (['sdk', 'audio'] if available else ['sdk'])


def test_server_prepares_voice_before_bluetooth_and_call_admission(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from mcp.server.fastmcp import FastMCP
    from hfp_mcp import server
    from hfp_mcp.routing import RoutingConfig
    from hfp_mcp.settings import RuntimeConfig
    from tests.test_phone_routing import routing_data

    order = []
    async def warmup(): order.append('warmup')
    async def bluetooth(): order.append('bluetooth')
    class Controller:
        def start(self): order.append('admit_calls')
        async def close(self): pass
    monkeypatch.setattr(gemini_live, 'warmup', warmup)
    monkeypatch.setattr(RoutingConfig, 'load', lambda: RoutingConfig.parse(routing_data()))
    monkeypatch.setattr(server, '_start_bluetooth_stack', bluetooth)
    monkeypatch.setattr(server, '_stop_bluetooth_stack', lambda: None)
    monkeypatch.setattr(server, '_create_phone_controller', lambda _: Controller())
    monkeypatch.setattr(server, '_phone_controller', None)
    # MCP session managers are single-use; this startup has its own instance.
    monkeypatch.setattr(server, 'mcp', FastMCP('startup-test'))
    config = RuntimeConfig.load(environ={'HFP_MCP_CONFIG': str(tmp_path / 'missing.yaml')})
    app = server.create_http_app(config, 't' * 40)
    with TestClient(app):
        assert order == ['warmup', 'bluetooth', 'admit_calls']


def test_probe_distinguishes_captured_from_submitted_signal_without_audio_retention():
    probe = StartupAudioProbe()
    loud = struct.pack('<160h', *([1000] * 160))
    quiet = bytes(320)
    for t in range(0, 200, 20):
        probe.observe('captured', loud, 8000, t)
        probe.observe('submitted', quiet, 8000, t)
    for t in range(200, 720, 20):
        probe.observe('captured', quiet, 8000, t)
    probe.provider_ready(40, 5)
    result = probe.snapshot()
    assert result['events'] == [
        {'source': 'captured', 'event': 'activity_start', 'elapsed_ms': 0},
        {'source': 'captured', 'event': 'activity_end', 'elapsed_ms': 200},
    ]
    assert result['sources']['captured']['above_threshold_ms'] == 200
    assert result['sources']['submitted']['above_threshold_ms'] == 0
    assert result['dropped_before_provider_ready'] == {'overflow_frames': 40, 'trimmed_frames': 5}
    assert loud not in probe.__dict__.values()


def test_probe_ignores_brief_clicks_and_stops_after_startup_window():
    probe = StartupAudioProbe()
    loud = struct.pack('<160h', *([1000] * 160))
    probe.observe('captured', loud, 8000, 0)
    for t in range(20, 800, 20):
        probe.observe('captured', bytes(320), 8000, t)
    assert probe.snapshot()['events'] == []
    before = probe.snapshot()
    for t in range(30001, 32000, 20):
        probe.observe('captured', loud, 8000, t)
    assert probe.snapshot() == before


def test_new_call_resets_diagnostics_and_transcript_timing_retains_no_text():
    manager = gemini_live.GeminiLiveManager(ensure_stream=None, clear_playback=None, hangup=None)
    from types import SimpleNamespace
    manager._handle_transcriptions(SimpleNamespace(input_transcription=SimpleNamespace(text='private words', finished=True)))
    assert 'first_input_transcription' in manager._timings
    assert 'private words' not in str(manager._timings)
    manager._startup_probe.provider_ready(40, 5)
    manager._reset_call_metrics()
    assert manager._timings == {}
    assert manager._startup_probe.snapshot()['dropped_before_provider_ready'] is None
