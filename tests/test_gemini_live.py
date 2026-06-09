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
        "start_gemini_live_call",
        "stop_gemini_live_call",
        "get_gemini_live_status",
        "send_gemini_live_text",
        "poll_gemini_live_requests",
        "submit_gemini_live_result",
    } <= tools


def test_pcm_resampler_changes_sample_rate_size():
    resampler = gemini_live.PcmResampler(8000, 16000)

    converted = resampler.convert(b"\x00\x00" * 80)

    assert len(converted) > 160
