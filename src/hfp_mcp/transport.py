"""
Transport wiring helpers.

FastMCP.run() takes only (transport, mount_path) — it does NOT accept host/port.
The bind address/port for network transports must be set on FastMCP's settings
object *before* run() is called.  This helper isolates that detail so it can be
unit-tested without importing the (hardware-dependent) server module.
"""

from __future__ import annotations


def apply_network_settings(settings, host: str, port: int) -> None:
    """
    Set the bind host/port on a FastMCP settings object for a network transport.

    Must be called before FastMCP.run("streamable-http"); passing host/port to
    run() raises TypeError (see tests/test_transport.py).
    """
    settings.host = host
    settings.port = port
