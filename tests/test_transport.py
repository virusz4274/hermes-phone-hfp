"""
Regression tests for the MCP transport wiring.

These guard the bug where the server crashed on startup in network mode because
host/port were passed to FastMCP.run() (which does not accept them).  They run
without Bluetooth/audio hardware — hfp_mcp.transport imports nothing heavy, and
mcp.server.fastmcp is a pure-Python import.
"""

import inspect

from hfp_mcp.transport import apply_network_settings


def test_apply_network_settings_sets_host_and_port():
    class _Settings:
        host = "127.0.0.1"
        port = 8000

    s = _Settings()
    apply_network_settings(s, "0.0.0.0", 9123)
    assert s.host == "0.0.0.0"
    assert s.port == 9123


def test_fastmcp_run_does_not_accept_host_or_port():
    """
    Contract guard: this is *why* apply_network_settings exists.  If a future SDK
    or a refactor reintroduces `mcp.run("streamable-http", host=..., port=...)`,
    that call raises TypeError at runtime; this test documents the constraint so
    the network-mode startup crash cannot silently come back.
    """
    from mcp.server.fastmcp import FastMCP

    params = inspect.signature(FastMCP.run).parameters
    assert "host" not in params, "FastMCP.run() must not be called with host="
    assert "port" not in params, "FastMCP.run() must not be called with port="
