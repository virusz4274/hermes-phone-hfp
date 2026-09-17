"""Read-only checks for the Hermes interfaces used by the phone plugin."""

from __future__ import annotations

import importlib
import shutil

TESTED_HERMES_COMMIT = "819988acb750836387fbb9d5d76203a9b3f530f4"
REQUIRED_RUN_FEATURES = frozenset(
    {
        "run_submission",
        "run_events_sse",
        "run_stop",
        "run_status",
        "run_approval_response",
    }
)
SESSION_METHODS = (
    "create_session",
    "get_session",
    "resolve_resume_session_id",
    "get_messages_as_conversation",
    "delete_session",
    "close",
)


def require_plugin_context(ctx):
    missing = [
        name
        for name in ("register_platform_handler", "register_hook", "register_tool")
        if not callable(getattr(ctx, name, None))
    ]
    if missing or not hasattr(ctx, "profile_name"):
        raise RuntimeError(
            "Incompatible Hermes plugin API; install the revision in docs/compatibility.md"
        )


def native_readiness(adapter=None) -> dict:
    missing = []
    try:
        db = importlib.import_module("hermes_state").SessionDB
        missing = [
            f"SessionDB.{name}"
            for name in SESSION_METHODS
            if not callable(getattr(db, name, None))
        ]
    except (ImportError, AttributeError):
        missing = ["SessionDB"]
    session_ok = not missing
    adapter_ok = adapter is not None and callable(
        getattr(adapter, "_profile_scope", None)
    )
    compact_ok = False
    if session_ok and adapter_ok and callable(getattr(adapter, "_create_agent", None)):
        try:
            agent = importlib.import_module("run_agent").AIAgent
            compact_ok = all(
                callable(getattr(agent, name, None))
                for name in ("_compress_context", "close")
            )
        except (ImportError, AttributeError):
            pass
    return {
        "sessions": session_ok and adapter_ok,
        "compaction": compact_ok,
        "missing": missing + ([] if adapter_ok else ["profile adapter"]),
    }


def speech_readiness() -> dict:
    """Inspect local shims only: no synthesis, model downloads, or credential refresh."""
    result = {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "transcription": False,
        "tts": False,
        "provider_validation": "not_performed",
    }
    for key, module, function in (
        ("transcription", "tools.transcription_tools", "transcribe_audio"),
        ("tts", "tools.tts_tool", "text_to_speech_tool"),
    ):
        try:
            result[key] = callable(
                getattr(importlib.import_module(module), function, None)
            )
        except (ImportError, AttributeError):
            pass
    return result
