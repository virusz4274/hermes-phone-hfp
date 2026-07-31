import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hfp_mcp import gemini_live


def _server_tool_names(env: dict | None = None, extra_path: Path | None = None) -> set[str]:
    repo_root = Path(__file__).resolve().parents[1]
    pythonpath = [str(repo_root / "src")]
    if extra_path is not None:
        pythonpath.insert(0, str(extra_path))
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from hfp_mcp import server; "
                "print(json.dumps(sorted(server.mcp._tool_manager._tools)))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **(env or {}),
            "PYTHONPATH": ":".join(pythonpath),
            "PATH": "/usr/bin:/bin",
        },
    )
    return set(json.loads(proc.stdout))


def _fake_gemini_deps(tmp_path: Path) -> None:
    google = tmp_path / "google"
    google.mkdir()
    (google / "__init__.py").write_text("")
    (google / "genai.py").write_text(
        textwrap.dedent(
            """
            class Client:
                def __init__(self, *args, **kwargs):
                    pass
            class types:
                pass
            """
        )
    )
    (tmp_path / "aiohttp.py").write_text("")


def test_gemini_live_availability_disabled(monkeypatch):
    monkeypatch.delenv("HFP_GEMINI_LIVE_ENABLED", raising=False)

    assert gemini_live.availability()["reason"] == "disabled"


def test_gemini_tools_absent_when_disabled():
    tools = _server_tool_names({})

    assert "start_gemini_live_call" not in tools
    assert "get_gemini_live_status" not in tools
    assert {
        "get_capabilities",
        "start_live_ai_call",
        "get_live_ai_status",
        "send_live_instruction",
        "speak_to_caller",
        "poll_live_ai_requests",
        "get_call_transcript",
    } <= tools


def test_gemini_tools_absent_without_api_key():
    tools = _server_tool_names({"HFP_GEMINI_LIVE_ENABLED": "true"})

    assert "start_gemini_live_call" not in tools


def test_gemini_tools_present_when_enabled_configured_and_dependencies_exist(tmp_path):
    _fake_gemini_deps(tmp_path)

    tools = _server_tool_names(
        {
            "HFP_GEMINI_LIVE_ENABLED": "true",
            "HFP_GEMINI_API_KEY": "test-key",
        },
        tmp_path,
    )

    assert {
        "start_live_ai_call",
        "stop_live_ai_call",
        "get_live_ai_status",
        "send_live_ai_text",
        "poll_live_ai_requests",
        "get_live_ai_pending_requests",
        "submit_live_ai_result",
        "cancel_live_request",
        "clear_live_requests",
        "get_call_transcript",
        "get_last_call_summary",
        "start_gemini_live_call",
        "stop_gemini_live_call",
        "get_gemini_live_status",
        "send_gemini_live_text",
        "poll_gemini_live_requests",
        "get_gemini_live_pending_requests",
        "submit_gemini_live_result",
    } <= tools


async def test_pending_gemini_requests_remain_visible_after_poll():
    async def ok(*_args):
        return {"ok": True}

    manager = gemini_live.GeminiLiveManager(
        ensure_stream=ok,
        clear_playback=ok,
        hangup=ok,
    )
    request = gemini_live.GeminiRequest(
        request_id="req-1",
        function_call_id="fc-1",
        name="ask_mcp_client",
        arguments={"task": "check status"},
        created_at=123.0,
    )
    await manager._requests.put(request)

    first = await manager.poll_requests(timeout_seconds=0.01)
    second = await manager.poll_requests(timeout_seconds=0.01)
    pending = manager.pending_requests()

    assert first["requests"] == [request.to_dict()]
    assert second["requests"] == []
    assert pending["requests"] == [request.to_dict()]
    assert manager.status()["pending_requests"] == 1
    assert manager.status()["queued_requests"] == 0
    assert manager.status()["total_unresolved_requests"] == 1


def test_gemini_function_declarations_include_generic_mcp_client_tools():
    names = {item["name"] for item in gemini_live._function_declarations()}

    assert {
        "ask_mcp_client",
        "notify_mcp_client",
        "get_mcp_client_context",
        "handoff_to_mcp_client",
    } <= names
    assert not ({"ask_hermes", "notify_hermes", "get_hermes_context", "handoff_to_hermes"} & names)


def test_pcm_resampler_changes_sample_rate_size():
    resampler = gemini_live.PcmResampler(8000, 16000)

    converted = resampler.convert(b"\x00\x00" * 80)

    assert len(converted) > 160
