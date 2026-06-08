"""
pre_llm_call hook — injects phone-call awareness into every LLM turn.

Same-machine mode (default):
  Reads /tmp/hfp-mcp-state.json written by hfp-mcp-server on every state change.

Remote mode (Pi MCP server, Hermes on another machine):
  Set HFP_MCP_STATUS_URL=http://raspberrypi.local:8001/status
  The hook fetches JSON from the Pi's status endpoint (1 s timeout, non-blocking
  to the LLM turn).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

_STATE_FILE = Path("/tmp/hfp-mcp-state.json")
_STATUS_URL = os.environ.get("HFP_MCP_STATUS_URL", "").strip()

# States where the user (or AI) is actively engaged in a call
_ACTIVE_CALL_STATES = {"dialing", "ringing", "active", "ending"}


def _fetch_state() -> dict | None:
    """Return the HFP state dict, or None if unreachable / not running."""
    if _STATUS_URL:
        try:
            with urllib.request.urlopen(_STATUS_URL, timeout=1.0) as resp:
                return json.loads(resp.read())
        except Exception:
            return None
    else:
        try:
            return json.loads(_STATE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None


def pre_llm_call_hook(*args, **kwargs) -> dict | None:
    """
    Return {"context": "<call info>"} while a phone call is in progress,
    None otherwise (Hermes ignores None returns from pre_llm_call hooks).
    """
    data = _fetch_state()
    if data is None:
        return None

    call_state = data.get("call_state", "idle")
    if call_state not in _ACTIVE_CALL_STATES:
        return None

    address = data.get("connected_address") or "unknown device"
    duration = data.get("call_duration_seconds")

    lines = [
        "[PHONE CALL IN PROGRESS]",
        f"  Status  : {call_state}",
        f"  Device  : {address}",
    ]
    if duration is not None:
        mins, secs = divmod(int(duration), 60)
        lines.append(f"  Duration: {mins}m {secs}s")

    lines.append(
        "You are assisting with an active Bluetooth phone call. "
        "Keep responses brief and suitable for voice output."
    )

    return {"context": "\n".join(lines)}
