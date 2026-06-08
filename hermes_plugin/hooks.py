"""
pre_llm_call hook — injects phone-call awareness into every LLM turn.

The hfp-mcp-server writes /tmp/hfp-mcp-state.json whenever the HFP
state changes.  We read that file here (fast, no network, no IPC) and
return a context string only when a call is actually in progress.
"""

from __future__ import annotations

import json
from pathlib import Path

_STATE_FILE = Path("/tmp/hfp-mcp-state.json")

# States where the user (or AI) is actively engaged in a call
_ACTIVE_CALL_STATES = {"dialing", "ringing", "active", "ending"}


def pre_llm_call_hook(*args, **kwargs) -> dict | None:
    """
    Return {"context": "<call info>"} while a phone call is in progress,
    None otherwise (Hermes ignores None returns from pre_llm_call hooks).
    """
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    call_state = data.get("call_state", "idle")
    if call_state not in _ACTIVE_CALL_STATES:
        return None

    address = data.get("connected_address") or "unknown device"
    duration = data.get("call_duration_seconds")

    lines = [
        f"[PHONE CALL IN PROGRESS]",
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
