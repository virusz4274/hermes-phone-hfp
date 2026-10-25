"""Read-only checks for the Hermes interfaces used by the phone plugin."""

from __future__ import annotations

import importlib
import shutil


def gateway_destination() -> dict:
    """Resolve Hermes's default home, without inheriting the phone/API origin."""
    try:
        from cron.scheduler_delivery import _resolve_delivery_target, cron_delivery_targets
        target = _resolve_delivery_target({'deliver': 'origin', 'origin': None})
        eligible = {t['id'] for t in cron_delivery_targets() if t.get('home_target_set')}
        if not target or target.get('platform') not in eligible:
            raise ValueError('Hermes has no available default gateway home destination')
        platform, chat = target['platform'], str(target['chat_id'])
        thread = target.get('thread_id')
        return {'platform': platform, 'chat_id': chat,
                'thread_id': str(thread) if thread is not None else None,
                'deliver': platform + ':' + chat + (':' + str(thread) if thread is not None else '')}
    except (ImportError, AttributeError) as exc:
        raise RuntimeError('Hermes gateway destination resolution is unavailable; upgrade Hermes') from exc


def validate_gateway_destination(destination: dict) -> None:
    """Check transport availability without changing a pinned recipient."""
    from cron.scheduler_delivery import cron_delivery_targets
    if not any(t['id'] == destination['platform'] for t in cron_delivery_targets()):
        raise ValueError('The scheduled Hermes gateway is no longer configured or enabled')

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


def memory_readiness(adapter=None) -> bool:
    """Inspect the tools-disabled auxiliary path; never invoke a provider."""
    if adapter is None or not callable(getattr(adapter, "_profile_scope", None)):
        return False
    try:
        return callable(getattr(importlib.import_module("agent.auxiliary_client"), "async_call_llm", None))
    except (ImportError, AttributeError):
        return False
