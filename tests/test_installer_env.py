"""Tests for installer-generated hfp-mcp service defaults."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_HELPER_PATH = Path(__file__).resolve().parents[1] / "setup" / "render_hfp_env.py"
_SPEC = importlib.util.spec_from_file_location("render_hfp_env", _HELPER_PATH)
render_hfp_env = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(render_hfp_env)


def test_choose_public_host_prefers_non_loopback_ipv4():
    host = render_hfp_env.choose_public_host(
        "127.0.0.1 192.168.1.42 10.0.0.9",
        "hfp-pi.local",
    )

    assert host == "192.168.1.42"


def test_choose_public_host_falls_back_to_fqdn_then_mdns_name():
    assert render_hfp_env.choose_public_host("", "hfp-pi.local") == "hfp-pi.local"
    assert render_hfp_env.choose_public_host("", "localhost") == "raspberrypi.local"


def test_render_env_exposes_remote_lan_audio_and_mcp_defaults():
    content = render_hfp_env.render_env("192.168.1.42")

    assert "--port 8000" in content
    assert "--status-port 8001" in content
    assert "--audio-host 0.0.0.0" in content
    assert "--audio-public-host 192.168.1.42" in content
    assert "--allowed-host 192.168.1.42:8000" in content
