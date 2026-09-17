"""
FastMCP server: HFP phone calling over Bluetooth.

Threading model
───────────────
Main thread   asyncio event loop (FastMCP / anyio)
  ├─ MCP stdio I/O
  ├─ Tool coroutines (dial, hangup, …)
  ├─ _run_handshake_and_session() coroutine
  └─ ATEventDispatcher asyncio Task

Thread "glib-mainloop"
  └─ GLib.MainLoop → dbus-python dispatches D-Bus signals
        └─ HFPProfile.NewConnection → loop.call_soon_threadsafe(...)

Thread "rfcomm-io"  (daemon)
  ├─ blocking socket.recv → loop.call_soon_threadsafe(queue.put_nowait, event)
  └─ writer: asyncio.run_coroutine_threadsafe(queue.get) → socket.sendall

Thread pool (asyncio run_in_executor)
  └─ blocking BlueZ D-Bus calls, SCO socket connect/teardown

Thread "sco-<session>"  (daemon, per audio session)
  └─ duplex SCO bridge: recv phone mic → capture ring; send playback → phone
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import AsyncIterator

import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .audio.sco import AudioManager, SCOAudioError, SCOHealthEvent, STREAM_FRAME_BYTES
from .audio.sidecar import (
    TOKEN_TTL_SECONDS,
    AudioStreamServer,
    StreamGrant,
)
from .bluez.agent import HFPAgent, register_agent, unregister_agent
from .bluez.manager import BlueZManager
from .bluez.profile import HFPProfile, register_hfp_profile, unregister_hfp_profile
from .config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    AUDIO_STREAM_FRAME_MS,
    AUDIO_STREAM_HOST,
    AUDIO_STREAM_PORT,
)
from .contracts import (
    ContractError,
    RequestLedger,
    failure,
    normalize_phone_number,
    ok,
    validate_mac,
    validate_request_id,
    validate_session_id,
    validate_timeout,
)
from .enrollment import authorize_enrollment_address
from .gemini_live import (
    HERMES_TOOL_NAMES,
    GeminiLiveManager,
    availability as gemini_live_availability,
    gemini_live_model,
)
from .hfp.handshake import HFPHandshaker, HandshakeError
from .hfp.protocol import CMD_ATA, CMD_ATD, CMD_CHUP
from .hfp.session import (
    ATCommandError,
    ATCommandBroker,
    ATCommandTimeout,
    ATEventDispatcher,
    CallInfo,
    RFCOMMThread,
    get_at_command_broker,
    reset_at_command_broker,
)
from .media import MediaLease, MediaLeaseManager
from .security import (
    BearerAuthMiddleware,
    HostOriginMiddleware,
    load_or_create_token,
    xdg_runtime_path,
)
from .settings import RuntimeConfig
from .state import CallDirection, CallState, ConnectionState, HFPState, SCOState
from .transport import apply_network_settings

log = logging.getLogger(__name__)

STATE_FILE = xdg_runtime_path("state.json")

# ---------------------------------------------------------------------------
# Singletons — initialised during lifespan
# ---------------------------------------------------------------------------

_state = HFPState()


def _on_sco_health(event: SCOHealthEvent) -> None:
    def _apply() -> None:
        lease = _media_leases.current()
        active_transport = _audio_manager.has_active_transport()
        if event.connected:
            if (
                active_transport
                and lease is not None
                and _state.call_state == CallState.ACTIVE
                and _state.call_id == lease.call_id
            ):
                _state.set_sco_state(
                    SCOState.READY,
                    bridge_state="ready",
                    owner=lease.owner,
                    stream_id=lease.stream_id,
                )
                _state.set_audio_active(True)
            return
        # A queued disconnect notification from a retired transport must not
        # clobber a replacement lease whose physical link is already healthy.
        if (
            active_transport
            and lease is not None
            and _state.call_state == CallState.ACTIVE
            and _state.call_id == lease.call_id
        ):
            _state.set_sco_state(
                SCOState.READY,
                bridge_state="ready",
                owner=lease.owner,
                stream_id=lease.stream_id,
            )
            return
        state = SCOState.FAILED if event.state == "failed" else SCOState.DISCONNECTED
        _state.set_sco_state(state, bridge_state=event.state)
        _state.set_audio_active(False)
        if event.state == "failed" and _audio_stream_server is not None:
            _audio_stream_server.detach_all()
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                failed_lease = _media_leases.current()
                failed_session = (
                    _audio_manager.get_session(failed_lease.stream_id)
                    if failed_lease is not None
                    else None
                )
                _archive_media_metrics(failed_lease, failed_session)
                _audio_manager.stop_all()
                _archive_media_metrics(failed_lease, failed_session)
                _media_leases.release()
                _legacy_stream_aliases.clear()
            else:
                asyncio.create_task(
                    _cleanup_all_audio_sessions(),
                    name="sco-failure-cleanup",
                )

    loop = _state._asyncio_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(_apply)
    else:
        _apply()


_audio_manager = AudioManager(_on_sco_health)
_media_leases = MediaLeaseManager()
_audio_stream_server: AudioStreamServer | None = None
_gemini_live_manager: GeminiLiveManager | None = None
_phone_controller = None
_gemini_allowed_tools: frozenset[str] | None = None
_manager: BlueZManager | None = None
_rfcomm_thread: RFCOMMThread | None = None
_dispatcher_task: asyncio.Task | None = None
_at_broker = None


@dataclass(frozen=True)
class RFCOMMConnectionContext:
    """All resources owned by one transferred RFCOMM connection."""

    address: str
    generation: int
    sock: object
    loop: asyncio.AbstractEventLoop
    event_queue: asyncio.Queue
    command_queue: asyncio.Queue
    broker: ATCommandBroker


_connection_context: RFCOMMConnectionContext | None = None
_session_tasks: dict[int, asyncio.Task] = {}
_rfcomm_threads: dict[int, RFCOMMThread] = {}
_dispatcher_tasks: dict[int, asyncio.Task] = {}

# Bluetooth stack — initialised once at process startup (see _start_bluetooth_stack)
_glib_loop: "GLib.MainLoop | None" = None
_glib_thread: threading.Thread | None = None
_system_bus = None
_control_bus = None
_bluez_owner_match = None
_agent_ref = None
_profile_ref = None
_bt_initialized = False
_bt_stopping = False
_bt_init_lock: "asyncio.Lock | None" = None
_http_mode = False
_runtime_config: RuntimeConfig | None = None
_request_ledger: RequestLedger | None = None
_control_token: str | None = None
_request_locks: dict[str, asyncio.Lock] = {}
_profile_lock_fd: int | None = None
_legacy_stream_aliases: dict[str, str] = {}
_last_ended_call_id: str | None = None
_last_ended_call_number: str | None = None
_last_ended_call_started_at: float | None = None
_last_ended_call_role: str = "unknown"
_last_call_media_metrics: dict | None = None
_call_media_metric_archive: dict[str, dict[str, dict]] = {}
_call_media_metric_archive_lock = threading.Lock()
_call_control_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_connection_control_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_audio_lifecycle_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_audio_reclaim_tasks: dict[str, asyncio.Task] = {}
_answered_call_token: tuple[int, int, str] | None = None
AUDIO_CLIENT_DISCONNECT_GRACE_SECONDS = 5.0
CALL_MEDIA_METRIC_ARCHIVE_MAX_CALLS = 16


def _loop_lock(
    locks: dict[asyncio.AbstractEventLoop, asyncio.Lock],
) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        locks[loop] = lock
    return lock


def _is_current_connection(context: RFCOMMConnectionContext) -> bool:
    return bool(
        _connection_context is context
        and _state.connection_generation == context.generation
    )


def _retire_connection_context(
    context: RFCOMMConnectionContext,
    reason: str,
    *,
    cancel_session: bool = True,
) -> None:
    """Stop only resources belonging to ``context``; never a replacement."""

    global _connection_context, _rfcomm_thread, _dispatcher_task, _at_broker
    context.broker.abort(reason)
    rfcomm_thread = _rfcomm_threads.pop(context.generation, None)
    if rfcomm_thread is not None:
        rfcomm_thread.stop()
    dispatcher_task = _dispatcher_tasks.pop(context.generation, None)
    if dispatcher_task is not None and not dispatcher_task.done():
        dispatcher_task.cancel()
    session_task = _session_tasks.pop(context.generation, None)
    if cancel_session and session_task is not None and not session_task.done():
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if session_task is not current_task:
            session_task.cancel()
    if _connection_context is context:
        _connection_context = None
        if _rfcomm_thread is rfcomm_thread:
            _rfcomm_thread = None
        if _dispatcher_task is dispatcher_task:
            _dispatcher_task = None
        if _at_broker is context.broker:
            _at_broker = None


def _audio_grant_is_current(session_id: str, grant: StreamGrant) -> bool:
    """Reject a token copied from an ended call or superseded media lease."""
    lease = _media_leases.current()
    return bool(
        _state.call_state == CallState.ACTIVE
        and _state.call_id
        and grant.call_id == _state.call_id
        and grant.server_instance_id == _state.server_instance_id
        and lease is not None
        and lease.stream_id == session_id
        and grant.generation == lease.generation
        and grant.owner == lease.owner
    )


def _sync_live_ai_state() -> None:
    _sync_audio_metrics()
    manager = _gemini_live_manager
    if manager is None:
        availability = gemini_live_availability()
        _state.set_live_ai_state(
            state="stopped",
            provider="gemini" if availability.get("available") else None,
            model=availability.get("model"),
            session_id=None,
            pending_tools=0,
        )
        _state.set_health(
            "gemini", "ready" if availability.get("available") else str(availability.get("reason", "disabled"))
        )
        return
    status = manager.status(_live_call_context())
    _state.set_live_ai_state(
        state=status.get("state", "stopped"),
        provider=status.get("provider"),
        model=status.get("model"),
        session_id=status.get("session_id"),
        pending_tools=status.get("total_unresolved_requests", 0),
        last_error=status.get("last_error"),
        reconnect_count=status.get("reconnect_count", 0),
    )
    _state.set_health(
        "gemini",
        "ok"
        if status.get("state") == "running"
        else str(status.get("last_error") or status.get("state") or "stopped"),
    )


def _media_metrics_for_lease(
    lease: MediaLease,
    session=None,
) -> dict | None:
    """Snapshot one exact lease even after it is no longer the current lease."""

    session = session or _audio_manager.get_session(lease.stream_id)
    if session is None:
        return None
    try:
        metrics = dict(session.media_metrics())
    except Exception:
        log.debug(
            "Could not snapshot audio metrics for stream %s",
            lease.stream_id,
            exc_info=True,
        )
        return None
    metrics.update(
        {
            "call_id": lease.call_id,
            "owner": lease.owner,
            "stream_id": lease.stream_id,
        }
    )
    return metrics


def _aggregate_media_metric_snapshots(
    call_id: str,
    streams: dict[str, dict],
) -> dict | None:
    """Combine replacement stream snapshots without double-counting a stream."""

    snapshots = list(streams.values())
    if not snapshots:
        return None
    if len(snapshots) == 1:
        return dict(snapshots[0])

    latest = snapshots[-1]
    aggregate: dict = {
        "call_id": call_id,
        "owner": latest.get("owner"),
        "stream_id": latest.get("stream_id"),
        "stream_ids": list(streams),
        "stream_count": len(streams),
    }
    ignored = {"call_id", "owner", "stream_id", "stream_ids", "stream_count"}
    keys = set().union(*(snapshot.keys() for snapshot in snapshots)) - ignored
    for key in keys:
        values = [snapshot[key] for snapshot in snapshots if key in snapshot]
        numeric = [
            value
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if len(numeric) == len(values):
            # Capacities and high-water marks describe the largest individual
            # stream. Counters, current queue depths, and durations are
            # additive across replacement streams belonging to one call.
            aggregate[key] = (
                max(numeric)
                if "capacity" in key or "peak" in key or "max" in key
                else sum(numeric)
            )
        elif values and all(isinstance(value, bool) for value in values):
            aggregate[key] = any(values)
        elif values:
            aggregate[key] = values[-1]
    return aggregate


def _archived_media_metrics(call_id: str | None) -> dict | None:
    if not call_id:
        return None
    with _call_media_metric_archive_lock:
        streams = _call_media_metric_archive.get(call_id)
        if streams is None:
            return None
        result = _aggregate_media_metric_snapshots(call_id, streams)
    return dict(result) if result is not None else None


def _archive_media_metrics(
    lease: MediaLease | None,
    session=None,
) -> dict | None:
    """Retain a bounded, idempotent per-call/per-stream diagnostic snapshot."""

    global _last_call_media_metrics
    if lease is None:
        return None
    snapshot = _media_metrics_for_lease(lease, session)
    if snapshot is None:
        return _archived_media_metrics(lease.call_id)
    with _call_media_metric_archive_lock:
        streams = _call_media_metric_archive.setdefault(lease.call_id, {})
        # Replacing the same stream makes the pre-detach and post-stop archive
        # hooks idempotent while retaining the final teardown accounting.
        streams[lease.stream_id] = snapshot
        while len(_call_media_metric_archive) > CALL_MEDIA_METRIC_ARCHIVE_MAX_CALLS:
            oldest_call_id = next(iter(_call_media_metric_archive))
            _call_media_metric_archive.pop(oldest_call_id, None)
        result = _aggregate_media_metric_snapshots(lease.call_id, streams)
        if result is not None and (
            _last_ended_call_id is None or _last_ended_call_id == lease.call_id
        ):
            _last_call_media_metrics = dict(result)
    return dict(result) if result is not None else None


def _current_media_metrics() -> dict | None:
    lease = _media_leases.current()
    return _media_metrics_for_lease(lease) if lease is not None else None


def _transport_metrics_for_call(call_id: str | None) -> dict | None:
    """Return current plus retired stream metrics for one physical call."""

    lease = _media_leases.current()
    if lease is not None and lease.call_id == call_id:
        _archive_media_metrics(lease)
    metrics = _archived_media_metrics(call_id)
    if metrics is not None:
        return metrics
    # Compatibility for callers/tests which still seed the former singleton.
    if (
        _last_call_media_metrics is not None
        and _last_call_media_metrics.get("call_id") == call_id
    ):
        return dict(_last_call_media_metrics)
    return None


def _sync_audio_metrics() -> dict | None:
    metrics = _current_media_metrics()
    if metrics is None:
        return None
    rejected = int(metrics.get("playback_rejected_bytes") or 0)
    _state.set_audio_metrics(
        queue_ms=float(metrics.get("playback_queue_ms") or 0.0),
        dropped_frames=(rejected + STREAM_FRAME_BYTES - 1) // STREAM_FRAME_BYTES,
        queue_peak_ms=float(metrics.get("playback_queue_peak_ms") or 0.0),
        overflow_events=int(metrics.get("playback_overflow_events") or 0),
        rejected_bytes=rejected,
        accepted_bytes=int(metrics.get("playback_accepted_bytes") or 0),
        consumed_bytes=int(metrics.get("playback_consumed_bytes") or 0),
        playback_controlled=bool(metrics.get("playback_controlled")),
        playback_target_ms=float(metrics.get("playback_target_ms") or 0.0),
        playback_underrun_events=int(
            metrics.get("playback_underrun_events") or 0
        ),
        playback_underrun_ms=float(
            metrics.get("playback_underrun_silence_ms") or 0.0
        ),
        capture_queue_ms=float(metrics.get("capture_queue_ms") or 0.0),
        capture_queue_peak_ms=float(
            metrics.get("capture_queue_peak_ms") or 0.0
        ),
        capture_overflow_bytes=int(metrics.get("capture_overflow_bytes") or 0),
    )
    return metrics


async def _run_idempotent_async(
    request_id: str,
    operation: str,
    handler,
) -> dict:
    request_id = validate_request_id(request_id)
    ledger = _request_ledger
    if ledger is None:
        return await handler()
    cached = ledger.get(request_id, operation)
    if cached is not None:
        return cached.response
    lock = _request_locks.setdefault(request_id, asyncio.Lock())
    try:
        async with lock:
            cached = ledger.get(request_id, operation)
            if cached is not None:
                return cached.response
            response = await handler()
            ledger.store(request_id, operation, response)
            return response
    finally:
        if not lock.locked():
            _request_locks.pop(request_id, None)


async def _run_ephemeral_idempotent_async(
    request_id: str,
    operation: str,
    handler,
) -> dict:
    """Replay an idempotent resource acquisition with fresh short-lived auth.

    Successful WebSocket grants must never be persisted: their token is
    single-use and would be both stale on retry and sensitive at rest.  The
    ledger stores only a completion marker, while the logical lease makes a
    replay safe and the handler issues a new one-time token.
    """
    request_id = validate_request_id(request_id)
    ledger = _request_ledger
    if ledger is None:
        return await handler()
    cached = ledger.get(request_id, operation)
    if cached is not None:
        return await handler() if cached.response.get("ok") else cached.response
    lock = _request_locks.setdefault(request_id, asyncio.Lock())
    try:
        async with lock:
            cached = ledger.get(request_id, operation)
            if cached is not None:
                return await handler() if cached.response.get("ok") else cached.response
            response = await handler()
            persisted = (
                {"ok": True, "result": {"resource": "duplex_audio_stream"}}
                if response.get("ok")
                else response
            )
            ledger.store(request_id, operation, persisted)
            return response
    finally:
        if not lock.locked():
            _request_locks.pop(request_id, None)


def _contract_failure(exc: Exception) -> dict:
    if isinstance(exc, ContractError):
        return failure(
            exc.code,
            exc.message,
            retryable=exc.retryable,
            state=_state.versioned_snapshot(),
        )
    return failure("internal_error", str(exc), state=_state.versioned_snapshot())


def _write_state_file() -> None:
    """Write a JSON snapshot of HFP state to STATE_FILE for the Hermes plugin."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")
        tmp.write_text(json.dumps(_state.versioned_snapshot()), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log.debug("State file write failed: %s", exc)


def _refresh_sco_connected() -> None:
    _state.set_sco_connected(_audio_manager.has_active_transport())


def _cancel_audio_reclaim(stream_id: str) -> None:
    task = _audio_reclaim_tasks.pop(stream_id, None)
    if task is None or task.done():
        return
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    if task is not current_task:
        task.cancel()


def _drop_media_aliases(stream_id: str) -> None:
    for alias, actual in tuple(_legacy_stream_aliases.items()):
        if actual == stream_id:
            _legacy_stream_aliases.pop(alias, None)


def _detach_logical_media_lease(expected: MediaLease) -> bool:
    """Synchronously revoke one lease before deferred physical teardown."""

    # Snapshot before revocation: an audio WebSocket/SCO failure can reclaim
    # the session before the later HFP call-ended metadata callback runs.
    _archive_media_metrics(expected)
    if _media_leases.release_if(expected) is None:
        return False
    _cancel_audio_reclaim(expected.stream_id)
    _drop_media_aliases(expected.stream_id)
    if _audio_stream_server is not None:
        _audio_stream_server.detach_session(expected.stream_id)
    return True


def _refresh_audio_state_after_cleanup() -> None:
    current = _media_leases.current()
    if current is not None and _audio_manager.has_active_transport():
        _state.set_sco_state(
            SCOState.READY,
            bridge_state="ready",
            owner=current.owner,
            stream_id=current.stream_id,
        )
        return
    _refresh_sco_connected()


async def _cleanup_released_media_lease(expected: MediaLease) -> bool:
    """Remove one exact logical audio session without touching successors."""

    async with _loop_lock(_audio_lifecycle_locks):
        session = _audio_manager.get_session(expected.stream_id)
        if session is not None:
            _archive_media_metrics(expected, session)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                _audio_manager.remove_session,
                expected.stream_id,
            )
            _archive_media_metrics(expected, session)
        _refresh_audio_state_after_cleanup()
    return True


async def _release_media_lease_exact(
    expected: MediaLease,
    *,
    already_detached: bool = False,
) -> bool:
    async with _loop_lock(_audio_lifecycle_locks):
        if not already_detached and not _detach_logical_media_lease(expected):
            return False
        session = _audio_manager.get_session(expected.stream_id)
        if session is not None:
            _archive_media_metrics(expected, session)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                _audio_manager.remove_session,
                expected.stream_id,
            )
            _archive_media_metrics(expected, session)
        _refresh_audio_state_after_cleanup()
        return True


def _arm_audio_lease_reclaim(expected: MediaLease, delay: float) -> None:
    """Reclaim an unattached/disconnected WebSocket lease after a grace period."""

    if _media_leases.current() != expected:
        return
    _cancel_audio_reclaim(expected.stream_id)

    async def _reclaim() -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            if _media_leases.current() != expected:
                return
            if (
                _audio_stream_server is not None
                and _audio_stream_server.client_attached(expected.stream_id)
            ):
                return
            await _release_media_lease_exact(expected)
            log.info(
                "Reclaimed abandoned audio lease %s for call %s",
                expected.stream_id,
                expected.call_id,
            )
        finally:
            task = _audio_reclaim_tasks.get(expected.stream_id)
            if task is asyncio.current_task():
                _audio_reclaim_tasks.pop(expected.stream_id, None)

    _audio_reclaim_tasks[expected.stream_id] = asyncio.create_task(
        _reclaim(),
        name=f"audio-reclaim-{expected.generation}",
    )


def _on_audio_client_connected(stream_id: str) -> None:
    loop = _state._asyncio_loop
    if loop is None or loop.is_closed():
        return

    def _connected() -> None:
        lease = _media_leases.current()
        if (
            lease is not None
            and lease.stream_id == stream_id
            and lease.call_id == _state.call_id
        ):
            _state.set_audio_client_attached(True, stream_id=stream_id)
            _cancel_audio_reclaim(stream_id)

    loop.call_soon_threadsafe(_connected)


def _on_audio_client_disconnected(stream_id: str) -> None:
    loop = _state._asyncio_loop
    if loop is None or loop.is_closed():
        return

    def _schedule() -> None:
        lease = _media_leases.current()
        if lease is not None and lease.stream_id == stream_id:
            _state.set_audio_client_attached(False, stream_id=stream_id)
            _arm_audio_lease_reclaim(
                lease,
                AUDIO_CLIENT_DISCONNECT_GRACE_SECONDS,
            )

    loop.call_soon_threadsafe(_schedule)


async def _cleanup_all_audio_sessions() -> int:
    async with _loop_lock(_audio_lifecycle_locks):
        lease = _media_leases.current()
        session = (
            _audio_manager.get_session(lease.stream_id)
            if lease is not None
            else None
        )
        _archive_media_metrics(lease, session)
        for stream_id in tuple(_audio_reclaim_tasks):
            _cancel_audio_reclaim(stream_id)
        if _audio_stream_server is not None:
            _audio_stream_server.detach_all()
        _media_leases.release()
        _legacy_stream_aliases.clear()
        loop = asyncio.get_running_loop()
        stopped = await loop.run_in_executor(None, _audio_manager.stop_all)
        _archive_media_metrics(lease, session)
        _refresh_sco_connected()
        return stopped


def _resolve_media_session_id(session_id: str, owner: str = "mcp_client") -> tuple[str, bool]:
    requested = validate_session_id(session_id)
    lease = _media_leases.current()
    if lease is not None:
        if requested == lease.stream_id or _legacy_stream_aliases.get(requested) == lease.stream_id:
            return lease.stream_id, True
        raise ContractError(
            "audio_owner_conflict",
            f"audio is already owned by {lease.owner}",
            retryable=True,
        )
    if _state.call_state != CallState.ACTIVE or not _state.call_id:
        raise ContractError("call_conflict", "no active call is available for audio")
    lease, _ = _media_leases.acquire(_state.call_id, owner)
    if requested != lease.stream_id:
        _legacy_stream_aliases[requested] = lease.stream_id
    return lease.stream_id, False


def _schedule_call_end_audio_cleanup(
    connection_generation: int | None = None,
    call_generation: int | None = None,
) -> None:
    if (
        connection_generation is not None
        and connection_generation != _state.connection_generation
    ) or (
        call_generation is not None
        and call_generation != _state.call_generation
    ):
        return
    live_session_id = None
    if _gemini_live_manager is not None:
        try:
            live_status = _gemini_live_manager.status()
            live_session_id = live_status.get("session_id")
            if live_status.get("running"):
                # Mark the close as intentional before detach_session closes
                # the HFP WebSocket. Full asynchronous teardown follows below.
                _gemini_live_manager.signal_stop("call_ended")
        except Exception:
            log.debug("Could not capture live session during call cleanup", exc_info=True)
    # Capture and revoke the ended call's logical lease synchronously.  The
    # slower SCO teardown may then run after a new call starts without blocking
    # or revoking that new call's lease.
    ended_lease = _media_leases.current()
    lease_detached = bool(
        ended_lease is not None and _detach_logical_media_lease(ended_lease)
    )

    loop = _state._asyncio_loop
    if loop is None or loop.is_closed():
        if _gemini_live_manager is not None:
            _gemini_live_manager.mark_session_stale("call_ended")
        if ended_lease is not None and lease_detached:
            ended_session = _audio_manager.get_session(ended_lease.stream_id)
            _audio_manager.remove_session(ended_lease.stream_id)
            _archive_media_metrics(ended_lease, ended_session)
        _refresh_audio_state_after_cleanup()
        return

    def _create_cleanup_task() -> None:
        async def _exact_cleanup() -> None:
            if _gemini_live_manager is not None:
                status = _gemini_live_manager.status()
                if live_session_id and status.get("session_id") == live_session_id:
                    await _gemini_live_manager.stop("call_ended", False)
            if ended_lease is not None and lease_detached:
                await _release_media_lease_exact(
                    ended_lease,
                    already_detached=True,
                )

        asyncio.create_task(_exact_cleanup(), name="audio-cleanup")

    loop.call_soon_threadsafe(_create_cleanup_task)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _start_bluetooth_stack() -> None:
    """
    Initialise the D-Bus / BlueZ side: adapter, pairing agent, HFP profile,
    and the GLib main loop that dispatches D-Bus signals.

    Idempotent — safe to call from both the stdio lifespan and the HTTP app
    startup hook. The work runs exactly once, on whichever event loop is
    serving MCP tool calls (the asyncio queues must bind to that loop).
    """
    global _manager, _glib_loop, _glib_thread, _system_bus, _bluez_owner_match
    global _control_bus
    global _agent_ref, _profile_ref, _profile_lock_fd
    global _bt_initialized, _bt_init_lock, _bt_stopping

    if _bt_init_lock is None:
        _bt_init_lock = asyncio.Lock()
    async with _bt_init_lock:
        if _bt_initialized:
            return
        _bt_stopping = False

        lock_path = xdg_runtime_path("profile-owner.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise RuntimeError(
                "another hfp-mcp daemon already owns the Bluetooth HFP profile; use its MCP endpoint"
            ) from exc
        _profile_lock_fd = lock_fd

        try:
            loop = asyncio.get_running_loop()

            # Queues MUST be created inside a running event loop (Python ≥ 3.10)
            _state._asyncio_loop = loop
            _state._at_event_queue = asyncio.Queue()
            _state._at_cmd_queue = asyncio.Queue()
            _state._on_change = _write_state_file
            _write_state_file()  # write initial disconnected state

            # Initialise D-Bus with GLib integration BEFORE creating the SystemBus
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            _system_bus = bus

            # Blocking control requests must not share the connection that
            # dispatches Profile1/Agent1 callbacks. BlueZ waits for our
            # NewConnection reply before completing ConnectProfile.
            _control_bus = dbus.SystemBus(
                private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP
            )
            _manager = BlueZManager(_control_bus)
            configured_adapter = (
                _runtime_config.adapter_address if _runtime_config else None
            )
            try:
                _manager.find_adapter(configured_adapter)
            except TypeError:
                # Compatibility with the original manager while upgrades roll.
                _manager.find_adapter()
            _manager.set_powered(True)
            _manager.set_connectable(True)
            _manager.set_pairable(False)

            # Pairing agent (Pi auto-accepts; phone shows passkey to user)
            configured_phone = (
                _runtime_config.device_address if _runtime_config else None
            )
            _agent_ref = HFPAgent(bus, configured_address=configured_phone)
            register_agent(bus)
        except BaseException:
            _stop_bluetooth_stack()
            raise

        # HFP profile — BlueZ calls NewConnection when the phone connects
        def _on_new_connection(address: str, sock, props: dict) -> None:
            expected = _runtime_config.device_address if _runtime_config else None
            if expected and address.upper() != expected.upper():
                log.warning("Rejecting HFP connection from unconfigured device %s", address)
                try:
                    sock.close()
                except Exception:
                    pass
                raise RuntimeError(f"unconfigured HFP device {address}")
            if not expected and not authorize_enrollment_address(address):
                log.warning("Rejecting HFP connection while enrollment is closed: %s", address)
                try:
                    sock.close()
                except Exception:
                    pass
                raise RuntimeError("configure HFP_PHONE_ADDRESS or open enrollment")

            def _accept_connection() -> None:
                global _connection_context, _at_broker
                previous = _connection_context
                if previous is not None:
                    _retire_connection_context(
                        previous,
                        "RFCOMM connection replaced",
                    )
                event_queue = asyncio.Queue(maxsize=512)
                command_queue = asyncio.Queue(maxsize=64)
                _state._at_event_queue = event_queue
                _state._at_cmd_queue = command_queue
                generation = _state.set_connected(address, sock)
                broker = reset_at_command_broker(
                    _state,
                    command_queue=command_queue,
                )
                context = RFCOMMConnectionContext(
                    address=address,
                    generation=generation,
                    sock=sock,
                    loop=loop,
                    event_queue=event_queue,
                    command_queue=command_queue,
                    broker=broker,
                )
                _connection_context = context
                _at_broker = broker
                task = asyncio.create_task(
                    _run_handshake_and_session(context),
                    name=f"hfp-session-{generation}",
                )
                _session_tasks[generation] = task

                def _forget_session(completed: asyncio.Task) -> None:
                    if _session_tasks.get(generation) is completed:
                        _session_tasks.pop(generation, None)

                task.add_done_callback(_forget_session)

            loop.call_soon_threadsafe(_accept_connection)

        def _on_request_disconnection(address: str) -> None:
            log.info("Phone requested disconnection: %s", address)
            def _disconnect() -> None:
                context = _connection_context
                generation = _state.connection_generation
                if context is not None and context.address.upper() == address.upper():
                    generation = context.generation
                    _retire_connection_context(
                        context,
                        "BlueZ requested disconnection",
                    )
                if (
                    _state.connected_address
                    and _state.connected_address.upper() == address.upper()
                    and _state.set_disconnected(generation)
                ):
                    _schedule_call_end_audio_cleanup()
            loop.call_soon_threadsafe(_disconnect)

        def _bluez_unavailable(reason: str) -> None:
            log.warning("BlueZ HFP ownership lost: %s", reason)
            if _bt_stopping:
                return

            def _release() -> None:
                context = _connection_context
                if context is not None:
                    _retire_connection_context(
                        context,
                        reason,
                    )
                _state.set_health("bluez", "released")
                _state.set_disconnected()
                _schedule_call_end_audio_cleanup()
                # BlueZ invalidates Profile1 registrations when bluetoothd is
                # replaced. Exit non-zero so systemd recreates the entire
                # D-Bus/profile stack instead of serving false health.
                if not _bt_stopping and os.getenv(
                    "HFP_EXIT_ON_BLUEZ_RELEASE", "true"
                ).lower() not in {
                    "0", "false", "no", "off"
                }:
                    loop.call_later(0.25, os._exit, 75)
            loop.call_soon_threadsafe(_release)

        def _on_release() -> None:
            _bluez_unavailable("BlueZ released HFP profile")

        def _on_bluez_owner_changed(name, old_owner, new_owner) -> None:
            if (
                str(name) == "org.bluez"
                and str(old_owner)
                and not str(new_owner)
            ):
                _bluez_unavailable("org.bluez disappeared from the system bus")

        try:
            _profile_ref = HFPProfile(
                bus, _on_new_connection, _on_request_disconnection, _on_release
            )
            register_hfp_profile(bus)
            _bluez_owner_match = bus.add_signal_receiver(
                _on_bluez_owner_changed,
                signal_name="NameOwnerChanged",
                dbus_interface="org.freedesktop.DBus",
                bus_name="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                arg0="org.bluez",
            )
            _state.set_health("bluez", "profile_ready")

            # Start GLib MainLoop in background thread (owns all D-Bus I/O)
            _glib_loop = GLib.MainLoop()
            _glib_thread = threading.Thread(
                target=_glib_loop.run, daemon=True, name="glib-mainloop"
            )
            _glib_thread.start()
            log.info("GLib MainLoop started")

            _bt_initialized = True
        except BaseException:
            _stop_bluetooth_stack()
            raise


def _stop_bluetooth_stack() -> None:
    """Tear down the GLib loop and audio. Called once at process shutdown."""
    global _glib_loop, _glib_thread, _system_bus, _bluez_owner_match, _manager
    global _control_bus
    global _agent_ref, _profile_ref, _profile_lock_fd
    global _bt_initialized, _bt_init_lock, _bt_stopping
    global _rfcomm_thread, _dispatcher_task, _at_broker
    _bt_stopping = True
    context = _connection_context
    if context is not None:
        _retire_connection_context(context, "HFP server shutting down")
    if _profile_ref is not None:
        _profile_ref.close_all_connections()
    if _audio_stream_server is not None:
        _audio_stream_server.stop()
    shutdown_lease = _media_leases.current()
    shutdown_session = (
        _audio_manager.get_session(shutdown_lease.stream_id)
        if shutdown_lease is not None
        else None
    )
    _archive_media_metrics(shutdown_lease, shutdown_session)
    _audio_manager.stop_all()
    _archive_media_metrics(shutdown_lease, shutdown_session)
    if _audio_stream_server is not None:
        _audio_stream_server.detach_all()
    for reclaim_task in tuple(_audio_reclaim_tasks.values()):
        reclaim_task.cancel()
    _audio_reclaim_tasks.clear()
    _media_leases.release()
    _legacy_stream_aliases.clear()
    _state.set_sco_connected(False)
    _state.set_disconnected()
    _state.set_health("bluez", "stopped")

    # Explicitly release BlueZ ownership while the GLib dispatcher is alive.
    # Process exit would eventually do this, but orderly unregistering makes
    # in-process restarts deterministic and prevents stale default agents.
    if _system_bus is not None:
        if _bluez_owner_match is not None:
            with suppress(Exception):
                _bluez_owner_match.remove()
        if _profile_ref is not None:
            with suppress(Exception):
                unregister_hfp_profile(_system_bus)
        if _agent_ref is not None:
            with suppress(Exception):
                unregister_agent(_system_bus)
    for dbus_object in (_profile_ref, _agent_ref):
        if dbus_object is not None:
            with suppress(Exception):
                dbus_object.remove_from_connection()

    if _glib_loop is not None:
        _glib_loop.quit()
        _glib_loop = None
    if _glib_thread is not None and _glib_thread.is_alive():
        _glib_thread.join(timeout=2.0)
    _glib_thread = None
    for thread in tuple(_rfcomm_threads.values()):
        thread.stop()
        thread.join(timeout=2.0)
    _rfcomm_threads.clear()
    for task in (*_session_tasks.values(), *_dispatcher_tasks.values()):
        if not task.done():
            task.cancel()
    _session_tasks.clear()
    _dispatcher_tasks.clear()
    STATE_FILE.unlink(missing_ok=True)
    if _profile_lock_fd is not None:
        try:
            fcntl.flock(_profile_lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(_profile_lock_fd)
            _profile_lock_fd = None
    _manager = None
    if _control_bus is not None:
        with suppress(Exception):
            _control_bus.close()
    _control_bus = None
    _system_bus = None
    _bluez_owner_match = None
    _profile_ref = None
    _agent_ref = None
    _rfcomm_thread = None
    _dispatcher_task = None
    _at_broker = None
    _state._asyncio_loop = None
    _state._at_event_queue = None
    _state._at_cmd_queue = None
    _state._on_change = None
    _bt_initialized = False
    _bt_init_lock = None
    log.info("GLib MainLoop stopped")


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    # Runs immediately in stdio mode (one session at launch); in HTTP mode it
    # also runs per MCP session, but _start_bluetooth_stack is guarded so the
    # real work happens exactly once (the HTTP app startup hook calls it first).
    await _start_bluetooth_stack()
    try:
        yield
    finally:
        # HTTP mode tears down via the app shutdown hook (not per-session).
        if not _http_mode:
            _stop_bluetooth_stack()


async def _run_handshake_and_session(context: RFCOMMConnectionContext) -> None:
    global _rfcomm_thread, _dispatcher_task, _at_broker

    # RFCOMM thread must start BEFORE the handshake: the handshaker writes
    # commands to the captured command queue and reads responses from the
    # captured event queue.  A replacement connection cannot redirect any of
    # these immutable resources while this coroutine is suspended.
    generation = context.generation
    broker = context.broker
    rfcomm_thread = RFCOMMThread(
        _state,
        context.sock,
        generation=generation,
        loop=context.loop,
        event_queue=context.event_queue,
        command_queue=context.command_queue,
    )
    _rfcomm_threads[generation] = rfcomm_thread
    if _is_current_connection(context):
        _rfcomm_thread = rfcomm_thread
        _at_broker = broker
    rfcomm_thread.start()

    try:
        await HFPHandshaker(
            _state,
            broker=broker,
            generation=generation,
            event_queue=context.event_queue,
        ).run()
        if not _is_current_connection(context):
            raise HandshakeError("RFCOMM connection generation was replaced")
        log.info("HFP connected to %s", context.address)
    except asyncio.CancelledError:
        rfcomm_thread.stop()
        await asyncio.to_thread(rfcomm_thread.join, 2.0)
        raise
    except HandshakeError as exc:
        log.error("Handshake failed: %s", exc)
        _retire_connection_context(
            context,
            f"HFP handshake failed: {exc}",
            cancel_session=False,
        )
        await asyncio.to_thread(rfcomm_thread.join, 2.0)
        if _profile_ref is not None:
            try:
                _profile_ref.close_connection(
                    context.address,
                    expected_socket=context.sock,
                )
            except Exception:
                pass
        if _state.set_disconnected(generation):
            _schedule_call_end_audio_cleanup()
        return

    audit_call_id: str | None = None
    audit_caller_number: str | None = None
    audit_caller_role = "unknown"
    audit_started_at: float | None = None

    def _on_call_info(info: CallInfo) -> None:
        nonlocal audit_call_id, audit_caller_number, audit_caller_role, audit_started_at
        if _state.call_id:
            if audit_call_id != _state.call_id:
                audit_call_id = _state.call_id
                audit_started_at = time.time()
        if not info.number:
            # Outbound ATD identity is bound before the phone emits call
            # progress, so an early CIEV without CLCC still has an auditable
            # number and role.
            with _state._lock:
                if (
                    _state.remote_number
                    and _state.remote_number_source in {"clip", "clcc", "dialed"}
                ):
                    audit_caller_number = _state.remote_number
                    audit_caller_role = _state.caller_role
            return
        try:
            number = normalize_phone_number(
                info.number,
                _runtime_config.default_region if _runtime_config else None,
            )
        except ContractError:
            log.warning("Ignoring invalid caller ID from HFP: %r", info.number)
            _state.set_remote_identity(
                None,
                source="invalid",
                name=None,
                caller_role="unknown",
            )
            audit_caller_number = None
            audit_caller_role = "unknown"
            return
        raw_source = _state.remote_number_source or "clcc_unvalidated"
        source = raw_source.removesuffix("_unvalidated")
        direction = (
            "incoming"
            if getattr(info.direction, "name", "") == "INCOMING"
            else "outgoing"
            if getattr(info.direction, "name", "") == "OUTGOING"
            else None
        )
        _state.set_remote_identity(
            number,
            source=source,
            name=info.name,
            direction=direction,
            caller_role=_caller_role_for_number(number),
        )
        audit_caller_number = number
        audit_caller_role = _state.caller_role

    def _on_call_ended_info(info: CallInfo) -> None:
        global _last_ended_call_id, _last_ended_call_number, _last_ended_call_started_at
        global _last_ended_call_role
        global _last_call_media_metrics
        nonlocal audit_call_id, audit_caller_number, audit_caller_role, audit_started_at
        lease = _media_leases.current()
        call_id = audit_call_id or (lease.call_id if lease else None)
        caller_number = audit_caller_number
        if caller_number is None and info.number:
            try:
                caller_number = normalize_phone_number(
                    info.number,
                    _runtime_config.default_region if _runtime_config else None,
                )
            except ContractError:
                caller_number = None
        caller_role = (
            _caller_role_for_number(caller_number)
            if caller_number
            else audit_caller_role
        )
        _last_ended_call_id = call_id
        _last_ended_call_number = caller_number
        _last_ended_call_started_at = audit_started_at
        _last_ended_call_role = caller_role
        if lease is not None and lease.call_id == call_id:
            _archive_media_metrics(lease)
        archived_metrics = _archived_media_metrics(call_id)
        # A peer/SCO failure may have reclaimed the stream before HFP reports
        # call end. Never erase that earlier diagnostic snapshot with None.
        if archived_metrics is not None:
            _last_call_media_metrics = archived_metrics
        try:
            if _request_ledger is None:
                return
            _request_ledger.audit(
                "call_ended",
                "ok",
                call_id=call_id,
                caller_number=caller_number,
                role=caller_role,
                detail={"direction": getattr(info.direction, "name", "unknown").lower()},
            )
            if call_id:
                summary = "Call completed. No transcript was retained."
                if _gemini_live_manager is not None:
                    stats = _gemini_live_manager.status()
                    if stats.get("session_id") == call_id:
                        _request_ledger.audit("voice_metrics", "final", call_id=call_id, detail={
                            key: stats.get(key) for key in (
                                "timing_from_audio_acquisition_ms", "tool_bridge", "reconnect_count",
                                "input_queue_overflow_frames", "dropped_playback_frames",
                                "playback_queue_peak_ms", "transcript_storage_error",
                            )
                        })
                    caller_message = _gemini_live_manager.consume_summary_candidate(call_id)
                    if caller_message:
                        summary = f"Caller message: {_redact_summary_text(caller_message)}"
                    else:
                        live_summary = _gemini_live_manager.get_last_call_summary(call_id)
                        input_count = int(live_summary.get("input_event_count") or 0)
                        output_count = int(live_summary.get("output_event_count") or 0)
                        if input_count or output_count:
                            summary = (
                                "Gemini Live call completed with "
                                f"{input_count} caller turn(s) and {output_count} assistant turn(s). "
                                + ("Full transcript retention is enabled." if _runtime_config and _runtime_config.full_transcripts
                                   else "Transcript text was not retained.")
                            )
                _request_ledger.save_call_summary(
                    call_id,
                    summary,
                    caller_number=caller_number,
                    role=caller_role,
                    started_at=audit_started_at,
                )
        finally:
            audit_call_id = None
            audit_caller_number = None
            audit_caller_role = "unknown"
            audit_started_at = None

    dispatcher = ATEventDispatcher(
        _state,
        _schedule_call_end_audio_cleanup,
        broker=broker,
        on_call_info=_on_call_info,
        on_call_ended_info=_on_call_ended_info,
        generation=generation,
        event_queue=context.event_queue,
    )
    dispatcher_task = asyncio.create_task(
        dispatcher.run(),
        name=f"at-dispatcher-{generation}",
    )
    _dispatcher_tasks[generation] = dispatcher_task
    if _is_current_connection(context):
        _dispatcher_task = dispatcher_task

    def _forget_dispatcher(completed: asyncio.Task) -> None:
        if _dispatcher_tasks.get(generation) is completed:
            _dispatcher_tasks.pop(generation, None)
        if _connection_context is context and _state.connection_state == ConnectionState.DISCONNECTED:
            _retire_connection_context(
                context,
                "RFCOMM dispatcher stopped",
                cancel_session=False,
            )

    dispatcher_task.add_done_callback(_forget_dispatcher)
    try:
        await dispatcher.query_current_calls(timeout=3.0)
    except ATCommandError:
        # Some phones reject CLCC while idle; CLIP/CIEV remain authoritative.
        log.debug("Initial AT+CLCC query was not available", exc_info=True)


# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("HFP Phone Controller", lifespan=lifespan)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

def _package_version() -> str:
    try:
        return version("hfp-mcp")
    except PackageNotFoundError:
        return "0.1.0"


@mcp.tool()
def get_capabilities() -> dict:
    """Return this server's provider-neutral feature and workflow surface."""
    gemini_availability = gemini_live_availability()
    gemini_available = bool(gemini_availability.get("available"))
    full_transcripts = bool(_runtime_config and _runtime_config.full_transcripts)
    return {
        "ok": True,
        "server": "hermes-phone-hfp",
        "version": _package_version(),
        "schema_version": "hfp.v1",
        "features": {
            "phone_controller": _phone_controller is not None,
            "bluetooth_hfp": True,
            "cvsd_only": True,
            "realtime_audio_websocket": True,
            "exclusive_media_lease": True,
            "single_use_audio_tokens": True,
            "authenticated_control": True,
            "verified_caller_identity": True,
            "live_ai": True,
            "gemini_live": gemini_available,
            "live_ai_function_calls": True,
            "audio_file_playback": True,
            "legacy_base64_audio": True,
            "call_transcript_history": full_transcripts,
            "redacted_call_summaries": True,
        },
        "providers": {
            "gemini": {
                "available": gemini_available,
                "reason": gemini_availability.get("reason"),
                "model": gemini_availability.get("model") or gemini_live_model(),
            }
        },
        "preferred_workflows": {
            "outbound_call": [
                "get_phone_state",
                "ensure_phone_connected",
                "place_call",
                "acquire_audio_stream",
            ],
            "live_ai_tool_loop": [
                "poll_live_ai_requests",
                "submit_live_ai_result",
            ],
            "post_call": [
                "record_call_summary",
                "get_call_transcript",
                "get_last_call_summary",
                "clear_live_requests",
            ],
        },
        "deprecated_tools": [
            "connect_phone",
            "connect_and_wait",
            "dial",
            "dial_and_wait",
            "hangup",
            "start_audio_stream",
            "ensure_audio_stream",
            "start_audio_capture",
            "get_audio_chunk",
            "play_audio",
        ],
        "compatibility_aliases": {
            "start_gemini_live_call": "start_live_ai_call",
            "stop_gemini_live_call": "stop_live_ai_call",
            "get_gemini_live_status": "get_live_ai_status",
            "send_gemini_live_text": "send_live_ai_text",
            "poll_gemini_live_requests": "poll_live_ai_requests",
            "get_gemini_live_pending_requests": "get_live_ai_pending_requests",
            "submit_gemini_live_result": "submit_live_ai_result",
        },
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_phone_state() -> dict:
    """Return the stable versioned HFP control, call, audio, and health state."""
    _sync_live_ai_state()
    return ok(_state.versioned_snapshot())


def _redact_summary_text(value: str) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = re.sub(r"(?i)https?://\S+|www\.\S+", "[url]", text)
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", text)
    text = re.sub(r"(?<!\w)\+?[0-9][0-9 ()-]{6,}[0-9](?!\w)", "[number]", text)
    return text[:1024]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def record_call_summary(
    call_id: str,
    summary: str,
    role: str,
    request_id: str,
) -> dict:
    """Persist one bounded redacted call/message summary; never a transcript."""
    try:
        call_id = validate_session_id(call_id)
        request_id = validate_request_id(request_id)
        normalized_role = str(role or "unknown").strip().lower()
        if normalized_role not in {"admin", "trusted", "unknown", "blocked", "mcp_client"}:
            raise ContractError("invalid_argument", "unsupported caller role")
        if call_id not in {_state.call_id, _last_ended_call_id}:
            raise ContractError("call_conflict", "call_id is not active or recently ended")
        redacted = _redact_summary_text(summary)
        if not redacted:
            raise ContractError("invalid_argument", "summary is empty after redaction")

        async def _record() -> dict:
            if _request_ledger is None:
                return failure("storage_unavailable", "call summary ledger is unavailable")
            existing = _request_ledger.get_call_summary(call_id) or {}
            authoritative_role = (
                _state.caller_role
                if call_id == _state.call_id
                else _last_ended_call_role
            )
            caller_number = (
                _state.remote_number
                if call_id == _state.call_id
                else _last_ended_call_number
            )
            started_at = (
                time.time() - (_state.versioned_snapshot()["call"].get("duration_seconds") or 0)
                if call_id == _state.call_id
                else _last_ended_call_started_at
            )
            _request_ledger.save_call_summary(
                call_id,
                redacted,
                caller_number=caller_number or existing.get("caller_number"),
                role=authoritative_role,
                started_at=started_at or existing.get("started_at"),
            )
            _request_ledger.audit(
                "record_call_summary",
                "ok",
                call_id=call_id,
                caller_number=caller_number,
                role=authoritative_role,
                detail={"summary_length": len(redacted)},
            )
            return ok(
                {"call_id": call_id, "stored": True, "redacted": True},
                state=_state.versioned_snapshot(),
            )

        return await _run_idempotent_async(
            request_id,
            f"record_call_summary:{call_id}:{normalized_role}",
            _record,
        )
    except Exception as exc:
        return _contract_failure(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def ensure_phone_connected(
    address: str,
    request_id: str,
    timeout_seconds: float = 15.0,
) -> dict:
    """Idempotently connect the exact configured phone and await HFP readiness."""
    try:
        target = validate_mac(address)
        timeout = validate_timeout(timeout_seconds, maximum=120.0)
        configured = _runtime_config.device_address if _runtime_config else None
        if configured and target != validate_mac(configured):
            raise ContractError("wrong_device_connected", "requested phone is not the configured HFP device")

        async def _connect() -> dict:
            if _state.connection_state == ConnectionState.CONNECTED:
                if _state.connected_address != target:
                    return failure(
                        "wrong_device_connected",
                        f"connected to {_state.connected_address}, not {target}",
                        state=_state.versioned_snapshot(),
                    )
                return ok({"connected": True}, state=_state.versioned_snapshot())
            result = await connect_and_wait(target, timeout)
            if not result.get("ok"):
                return failure(
                    "timeout" if "Timed out" in str(result.get("error")) else "connection_failed",
                    str(result.get("error", "phone connection failed")),
                    retryable=True,
                    state=_state.versioned_snapshot(),
                )
            if _state.connected_address != target:
                return failure(
                    "wrong_device_connected",
                    f"BlueZ connected {_state.connected_address}, not {target}",
                    state=_state.versioned_snapshot(),
                )
            return ok({"connected": True}, state=_state.versioned_snapshot())

        return await _run_idempotent_async(
            request_id, f"ensure_phone_connected:{target}", _connect
        )
    except Exception as exc:
        return _contract_failure(exc)


def _reject_self_call(normalized_number: str) -> None:
    """Fail closed when an outbound target is the paired handset's own SIM."""

    if _runtime_config is None or not _runtime_config.self_number:
        return
    self_number = normalize_phone_number(
        _runtime_config.self_number,
        _runtime_config.default_region,
    )
    if normalized_number == self_number:
        raise ContractError(
            "self_call_blocked",
            "outbound target matches the configured paired-handset number",
        )


def _caller_role_for_number(normalized_number: str | None) -> str:
    """Classify one verified E.164 identity from daemon-owned configuration."""

    if _phone_controller is not None:
        route, reason = _phone_controller.config.resolve(normalized_number)
        if reason == "blocked":
            return "blocked"
        if route is None:
            return "unknown"
        return "admin" if _phone_controller.config.policies[route.policy].admin else "trusted"
    if not normalized_number or _runtime_config is None:
        return "unknown"
    region = _runtime_config.default_region

    def _matches(configured_numbers: tuple[str, ...]) -> bool:
        for configured in configured_numbers:
            try:
                if normalize_phone_number(configured, region) == normalized_number:
                    return True
            except ContractError:
                # RuntimeConfig validates these values. Keep classification
                # fail-closed if an embedded caller bypassed validation.
                continue
        return False

    if _matches(_runtime_config.blocked_callers):
        return "blocked"
    admin_numbers = _runtime_config.admin_callers
    if _runtime_config.owner_number:
        admin_numbers = (*admin_numbers, _runtime_config.owner_number)
    if _matches(admin_numbers):
        return "admin"
    if _matches(_runtime_config.trusted_callers):
        return "trusted"
    return "unknown"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
async def place_call(
    number: str,
    request_id: str,
    timeout_seconds: float = 30.0,
    cancel_on_timeout: bool = True,
) -> dict:
    """Idempotently place a call and await the HFP active-call indicator.

    Success confirms call control only. It does not acquire the media lease or
    prove that SCO audio, transcription, synthesis, or a live-AI provider is
    ready; clients must establish and verify their selected media path. Agents
    must not use this as an automatic retry after another dial tool fails;
    require a new user request before a second outbound attempt.
    """
    try:
        normalized = normalize_phone_number(
            number,
            _runtime_config.default_region if _runtime_config else None,
        )
        _reject_self_call(normalized)
        timeout = validate_timeout(timeout_seconds, maximum=180.0)

        async def _place() -> dict:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            result = await _dial_normalized(
                normalized,
                min(_configured_at_dial_timeout(), timeout),
            )
            if not result.get("ok"):
                return failure(
                    "call_conflict" if _state.call_state != CallState.IDLE else "command_rejected",
                    str(result.get("error", "dial command rejected")),
                    state=_state.versioned_snapshot(),
                )
            waited = await _wait_for_status(
                lambda state: state["call_state"] == CallState.ACTIVE.value,
                max(0.0, deadline - loop.time()),
            )
            if waited.get("ok"):
                return ok(
                    {"call_id": _state.call_id, "number": normalized},
                    state=_state.versioned_snapshot(),
                )
            intent = dict(result.get("_dial_intent") or {})
            progressed = _outgoing_call_for_intent(intent)
            cancellation_state = "not_requested"
            if cancel_on_timeout:
                cancellation_state = "cancelled"
                if progressed is not None:
                    cancelled = await hangup()
                    if cancelled.get("ok"):
                        await _wait_for_status(
                            lambda state: state["call_state"] == CallState.IDLE.value,
                            min(5.0, timeout),
                        )
                    elif getattr(
                        get_at_command_broker(_state),
                        "terminal_quarantined",
                        False,
                    ):
                        cancellation_state = "pending"
                        asyncio.create_task(
                            _cancel_late_dial_intent(
                                normalized,
                                intent,
                                _configured_at_dial_timeout(),
                            ),
                            name="hfp-cancel-quarantined-dial",
                        )
                    else:
                        cancellation_state = "failed"
                elif result.get("confirmation_pending"):
                    # A timed-out ATD remains ambiguous. Watch only this exact
                    # connection/call generation and cancel a genuinely late
                    # call without ever issuing a second ATD.
                    asyncio.create_task(
                        _cancel_late_dial_intent(
                            normalized,
                            intent,
                            _configured_at_dial_timeout(),
                        ),
                        name="hfp-cancel-late-dial",
                    )
                    cancellation_state = "pending"
                else:
                    _clear_dial_intent(normalized, intent)
            elif result.get("confirmation_pending"):
                asyncio.create_task(
                    _expire_pending_dial_intent(
                        normalized,
                        intent,
                        _configured_at_dial_timeout(),
                    ),
                    name="hfp-expire-place-call-intent",
                )
            error_code = "timeout"
            message = f"call did not become active within {timeout:g}s"
            if cancel_on_timeout:
                error_code = (
                    "timeout_cancel_pending"
                    if cancellation_state == "pending"
                    else "timeout_cancel_failed"
                    if cancellation_state == "failed"
                    else "timeout_cancelled"
                )
                if cancellation_state == "pending":
                    message += "; generation-scoped late-call cancellation is pending"
                elif cancellation_state == "failed":
                    message += "; call cancellation failed"
            return failure(
                error_code,
                message,
                retryable=True,
                state=_state.versioned_snapshot(),
            )

        operation = f"place_call:{normalized}:{timeout:g}:{int(cancel_on_timeout)}"
        response = await _run_idempotent_async(request_id, operation, _place)
        if _request_ledger is not None:
            _request_ledger.audit(
                "place_call",
                "ok" if response.get("ok") else "error",
                call_id=_state.call_id,
                caller_number=normalized,
                role=_caller_role_for_number(normalized),
            )
        return response
    except Exception as exc:
        return _contract_failure(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
async def end_call(
    call_id: str,
    request_id: str,
    wait: bool = True,
) -> dict:
    """Idempotently end the specified call and revoke all media access."""
    try:
        if not call_id:
            raise ContractError("invalid_argument", "call_id is required")

        async def _end() -> dict:
            if _state.call_state == CallState.IDLE:
                return ok({"ended": True, "already_idle": True}, state=_state.versioned_snapshot())
            if _state.call_id != call_id:
                return failure("call_conflict", "call_id is not the active call", state=_state.versioned_snapshot())
            call_lease = _media_leases.current()
            if call_lease is not None and call_lease.call_id != call_id:
                call_lease = None
            result = await hangup()
            if not result.get("ok"):
                return failure("command_rejected", str(result.get("error", "hangup rejected")), state=_state.versioned_snapshot())
            if wait:
                waited = await _wait_for_status(
                    lambda state: state["call_state"] == CallState.IDLE.value,
                    10.0,
                )
                if not waited.get("ok"):
                    return failure("timeout", "phone did not confirm call end", retryable=True, state=_state.versioned_snapshot())
            if call_lease is not None:
                await _release_media_lease_exact(call_lease)
            return ok({"ended": True}, state=_state.versioned_snapshot())

        return await _run_idempotent_async(
            request_id, f"end_call:{call_id}:{int(wait)}", _end
        )
    except Exception as exc:
        return _contract_failure(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def acquire_audio_stream(
    call_id: str,
    owner: str,
    request_id: str,
) -> dict:
    """Acquire the call's exclusive duplex media lease and return one-time auth."""
    try:
        validate_request_id(request_id)
        if _state.call_state != CallState.ACTIVE or _state.call_id != call_id:
            raise ContractError("call_conflict", "call_id is not the active call")

        async def _acquire() -> dict:
            lease, reused = _media_leases.acquire(call_id, owner)
            try:
                result = await start_audio_stream(lease.stream_id)
            except Exception:
                if not reused:
                    _media_leases.release(lease.stream_id)
                raise
            if not result.get("ok"):
                if not reused:
                    _media_leases.release(lease.stream_id)
                return failure("audio_unavailable", str(result.get("error", "SCO unavailable")), retryable=True, state=_state.versioned_snapshot())
            if (
                _media_leases.current() != lease
                or _state.call_state != CallState.ACTIVE
                or _state.call_id != call_id
            ):
                await _cleanup_released_media_lease(lease)
                return failure(
                    "call_conflict",
                    "call ended while audio was being acquired",
                    state=_state.versioned_snapshot(),
                )
            _state.set_sco_state(
                SCOState.READY,
                bridge_state="ready",
                owner=owner,
                stream_id=lease.stream_id,
            )
            return ok(
                {
                    "stream_id": lease.stream_id,
                    "stream_url": result["stream_url"],
                    "audio": result["audio"],
                    "lease_reused": reused,
                },
                state=_state.versioned_snapshot(),
            )

        operation = f"acquire_audio_stream:{call_id}:{owner}"
        return await _run_ephemeral_idempotent_async(request_id, operation, _acquire)
    except Exception as exc:
        return _contract_failure(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def release_audio_stream(stream_id: str) -> dict:
    """Release an active media lease and invalidate its WebSocket immediately."""
    try:
        stream_id = validate_session_id(stream_id)
        lease = _media_leases.current()
        if lease is None or lease.stream_id != stream_id:
            raise ContractError("stream_expired", "no active audio stream")
        if not await _release_media_lease_exact(lease):
            raise ContractError("stream_expired", "audio stream was superseded")
        return ok({"released": stream_id}, state=_state.versioned_snapshot())
    except Exception as exc:
        return _contract_failure(exc)

@mcp.tool()
async def scan_paired_devices() -> list[dict]:
    """
    List Bluetooth devices paired with this machine that support HFP
    (i.e. phones). Returns address, name, and whether currently connected.

    Recommended workflow:
    1. Use this once to find the phone address.
    2. Use connect_and_wait(address) to connect and wait for HFP readiness.
    """
    if _manager is None:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _manager.get_paired_hfp_devices)


def _validate_connect_target(address: str) -> str:
    address = validate_mac(address)
    configured = _runtime_config.device_address if _runtime_config else None
    if not configured:
        raise ContractError(
            "not_enrolled",
            "No enrolled phone; run hfp-mcp enroll",
        )
    if address != validate_mac(configured):
        raise ContractError(
            "device_conflict",
            "Requested phone is not the configured HFP device",
        )
    return address


async def _connect_phone_unlocked(address: str) -> dict:
    if _manager is None:
        return {"ok": False, "error": "Server not initialised"}
    if _state.connection_state != ConnectionState.DISCONNECTED:
        if (
            _state.connection_state == ConnectionState.CONNECTED
            and _state.connected_address == address
        ):
            return {"ok": True, "message": f"Already connected to {address}"}
        return {
            "ok": False,
            "error": f"Already connected to {_state.connected_address}",
        }
    try:
        generation = _state.set_connecting(address)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _manager.connect_device, address)
        return {"ok": True, "message": f"Connection initiated to {address}"}
    except Exception as exc:
        # The HFP handshake can complete before BlueZ acknowledges the request.
        # Its generation-safe state is authoritative even if the D-Bus reply
        # subsequently times out.
        if (
            _state.connection_state == ConnectionState.CONNECTED
            and _state.connected_address == address
        ):
            log.info(
                "HFP became ready for %s despite an incomplete BlueZ connect reply: %s",
                address,
                exc,
            )
            return {
                "ok": True,
                "message": f"HFP connected to {address}",
            }
        _state.set_disconnected(generation if "generation" in locals() else None)
        return {"ok": False, "error": str(exc)}


async def _recover_timed_out_connection(address: str) -> None:
    """Cancel only the still-pending attempt and restore retryable state."""

    with _state._lock:
        if (
            _state.connected_address != address
            or _state.connection_state
            not in {ConnectionState.CONNECTING, ConnectionState.HANDSHAKING}
        ):
            return
        generation = _state.connection_generation
    context = _connection_context
    if (
        context is not None
        and context.generation == generation
        and context.address == address
    ):
        _retire_connection_context(context, "HFP connection attempt timed out")
        if _profile_ref is not None:
            try:
                _profile_ref.close_connection(
                    address,
                    expected_socket=context.sock,
                )
            except Exception:
                log.debug("Timed-out RFCOMM close failed", exc_info=True)
    _state.set_disconnected(generation)
    if _manager is not None:
        try:
            await asyncio.to_thread(_manager.disconnect_device, address)
        except Exception:
            # ConnectProfile may never have reached a profile connection, in
            # which case BlueZ legitimately rejects DisconnectProfile.
            log.debug("Timed-out BlueZ connection cleanup was unavailable", exc_info=True)


@mcp.tool()
async def connect_phone(address: str) -> dict:
    """
    Connect to a paired Android phone by Bluetooth address (AA:BB:CC:DD:EE:FF).
    Triggers the HFP profile connection; the handshake runs automatically.

    Low-level tool: returns immediately. Agents should usually prefer
    connect_and_wait(address), which avoids repeated get_call_status polling.
    """
    try:
        address = _validate_connect_target(address)
    except ContractError as exc:
        return {"ok": False, "error": exc.message}
    async with _loop_lock(_connection_control_locks):
        return await _connect_phone_unlocked(address)


async def _wait_for_status(predicate, timeout_seconds: float, interval: float = 0.25) -> dict:
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout_seconds)
    last = _state.snapshot()
    while True:
        if predicate(last):
            return {"ok": True, "status": last}
        if asyncio.get_event_loop().time() >= deadline:
            return {
                "ok": False,
                "error": f"Timed out after {timeout_seconds:g}s",
                "status": last,
            }
        await asyncio.sleep(
            min(interval, max(0.0, deadline - asyncio.get_event_loop().time()))
        )
        last = _state.snapshot()


@mcp.tool()
async def connect_and_wait(address: str, timeout_seconds: float = 15.0) -> dict:
    """
    Connect to a paired phone and wait until the HFP service-level connection is
    ready. This collapses connect_phone + repeated get_call_status polling.

    Use this before dial_and_wait(), answer_call(), or ensure_audio_stream().
    Returns ok=true with status when connection=connected.
    """
    try:
        address = _validate_connect_target(address)
        timeout_seconds = validate_timeout(timeout_seconds, maximum=120.0)
    except ContractError as exc:
        return {"ok": False, "error": exc.message}

    async with _loop_lock(_connection_control_locks):
        if _state.connection_state == ConnectionState.CONNECTED:
            if _state.connected_address == address:
                return {"ok": True, "status": _state.snapshot()}
            return {
                "ok": False,
                "error": f"Connected to {_state.connected_address}, not {address}",
            }
        if (
            _state.connection_state
            not in {ConnectionState.CONNECTING, ConnectionState.HANDSHAKING}
            or _state.connected_address != address
        ):
            result = await _connect_phone_unlocked(address)
            if not result.get("ok"):
                return result

        waited = await _wait_for_status(
            lambda s: s["connection"] == ConnectionState.CONNECTED.value
            and s["connected_address"] == address,
            timeout_seconds,
        )
        if waited.get("ok"):
            return waited
        # Close/reset only if this attempt remains transitional.  A success on
        # the timeout boundary wins over cleanup after the final recheck.
        if (
            _state.connection_state == ConnectionState.CONNECTED
            and _state.connected_address == address
        ):
            return {"ok": True, "status": _state.snapshot()}
        await _recover_timed_out_connection(address)
        waited["status"] = _state.snapshot()
        waited["retryable"] = True
        return waited


@mcp.tool()
async def disconnect_phone() -> dict:
    """
    Disconnect the currently connected phone.

    This also clears call/audio state. Use hangup() first if a call is active
    and you want the phone call ended cleanly before Bluetooth disconnects.
    """
    async with _loop_lock(_connection_control_locks):
        if _manager is None:
            return {"ok": False, "error": "Server not initialised"}
        if _state.connection_state == ConnectionState.DISCONNECTED:
            return {"ok": False, "error": "No phone connected"}
        address = _state.connected_address
        generation = _state.connection_generation
        context = _connection_context
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _manager.disconnect_device, address)
            if context is not None and context.generation == generation:
                _retire_connection_context(context, "Phone disconnected locally")
            await _cleanup_all_audio_sessions()
            _state.set_disconnected(generation)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


@mcp.tool()
async def dial(number: str) -> dict:
    """
    Make an outgoing call to a phone number (e.g. "+14155551234").
    The phone must already be connected.

    Low-level tool: returns immediately. Agents should usually prefer
    dial_and_wait(number), which waits until call_state=active and
    audio_active=true before returning.
    """
    try:
        normalized = normalize_phone_number(
            number,
            _runtime_config.default_region if _runtime_config else None,
        )
        _reject_self_call(normalized)
    except (ContractError, ValueError, ATCommandError) as exc:
        if isinstance(exc, ContractError):
            return failure(exc.code, exc.message, retryable=exc.retryable)
        return {"ok": False, "error": str(exc)}

    result = await _dial_normalized(
        normalized,
        _configured_at_dial_timeout(),
    )
    if result.get("ok") and result.get("confirmation_pending"):
        intent = dict(result.get("_dial_intent") or {})
        asyncio.create_task(
            _expire_pending_dial_intent(
                normalized,
                intent,
                _configured_at_dial_timeout(),
            ),
            name="hfp-expire-ambiguous-dial-intent",
        )
    result.pop("_dial_intent", None)
    return result


def _configured_at_dial_timeout() -> float:
    return float(
        _runtime_config.at_dial_timeout_seconds
        if _runtime_config is not None
        else 15.0
    )


def _outgoing_call_for_intent(intent: dict) -> tuple[str | None, int] | None:
    """Return the exact progressed outbound call for a pre-ATD intent."""

    with _state._lock:
        if (
            _state.connection_generation
            != intent.get("connection_generation")
            or _state.call_generation != intent.get("call_generation", -1) + 1
            or _state.call_state == CallState.IDLE
            or _state.call_direction != CallDirection.OUTGOING
        ):
            return None
        return _state.call_id, _state.call_generation


def _clear_dial_intent(number: str, intent: dict) -> bool:
    if not intent:
        return False
    return _state.clear_pending_outbound_identity(
        number,
        expected_connection_generation=intent["connection_generation"],
        expected_call_generation=intent["call_generation"],
    )


async def _expire_pending_dial_intent(
    number: str,
    intent: dict,
    delay_seconds: float,
) -> None:
    await asyncio.sleep(max(0.0, delay_seconds))
    _clear_dial_intent(number, intent)


async def _cancel_late_dial_intent(
    number: str,
    intent: dict,
    grace_seconds: float,
) -> None:
    """Hang up a call that starts just after a cancelling caller timed out."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, grace_seconds)
    while loop.time() < deadline:
        if _outgoing_call_for_intent(intent) is not None:
            result = await hangup()
            if result.get("ok"):
                return
            broker = get_at_command_broker(_state)
            if not getattr(broker, "terminal_quarantined", False):
                return
            # No CHUP was sent while the prior bare terminal remained
            # ambiguous. Retry the cancellation only after it is consumed.
            await asyncio.sleep(0.1)
            continue
        with _state._lock:
            still_pending = bool(
                _state.connection_generation == intent.get("connection_generation")
                and _state.call_generation == intent.get("call_generation")
                and _state.call_state == CallState.IDLE
                and _state.remote_number == number
                and _state.remote_number_source == "dialed_pending"
            )
        if not still_pending:
            return
        await asyncio.sleep(0.1)
    _clear_dial_intent(number, intent)


async def _dial_normalized(
    normalized: str,
    command_timeout_seconds: float,
) -> dict:
    """Send one ATD while retaining enough state to reconcile a late reply."""

    async with _loop_lock(_call_control_locks):
        if _state.connection_state != ConnectionState.CONNECTED:
            return {"ok": False, "error": "Phone not connected (HFP not ready)"}
        if _state.call_state != CallState.IDLE:
            return {
                "ok": False,
                "error": f"Already in call state: {_state.call_state.value}",
            }
        connection_generation = _state.connection_generation
        call_generation = _state.call_generation
        intent = {
            "connection_generation": connection_generation,
            "call_generation": call_generation,
        }
        if not _state.set_pending_outbound_identity(
            normalized,
            caller_role=_caller_role_for_number(normalized),
            expected_connection_generation=connection_generation,
            expected_call_generation=call_generation,
        ):
            return {"ok": False, "error": "Call state changed before ATD"}
        ambiguous_timeout = False
        try:
            await get_at_command_broker(_state).execute(
                CMD_ATD.format(number=normalized),
                timeout=command_timeout_seconds,
            )
        except ATCommandTimeout:
            # ATD is not safely retryable: several phones begin the real call
            # before sending a late terminal OK. Preserve the single dial
            # intent so the caller can observe CIEV/CLCC through its deadline.
            ambiguous_timeout = True
        except ATCommandError as exc:
            progressed = _outgoing_call_for_intent(intent)
            if progressed is None:
                _clear_dial_intent(normalized, intent)
                return {"ok": False, "error": str(exc)}

        transitioned = False
        if not ambiguous_timeout:
            transitioned = _state.transition_call_state(
                CallState.DIALING,
                expected_call_id=None,
                expected_generation=call_generation,
                allowed_states={CallState.IDLE},
                direction=CallDirection.OUTGOING,
                expected_connection_generation=connection_generation,
            )
        progressed = _outgoing_call_for_intent(intent)
        current_call_id = progressed[0] if progressed is not None else None
        current_call_generation = progressed[1] if progressed is not None else None
        if transitioned:
            current_call_id = _state.call_id
            current_call_generation = _state.call_generation
        if current_call_id is not None and current_call_generation is not None:
            _state.set_remote_identity_if(
                current_call_id,
                current_call_generation,
                normalized,
                source="dialed",
                direction=CallDirection.OUTGOING,
                caller_role=_caller_role_for_number(normalized),
            )
        elif not ambiguous_timeout:
            _clear_dial_intent(normalized, intent)
            return {"ok": False, "error": "Call state changed while dialing"}
        return {
            "ok": True,
            "dialing": normalized,
            "call_id": current_call_id,
            "confirmation_pending": bool(
                ambiguous_timeout and current_call_id is None
            ),
            "_dial_intent": intent,
        }


@mcp.tool()
async def dial_and_wait(number: str, timeout_seconds: float = 30.0) -> dict:
    """
    Dial a number and wait until the call becomes active. Physical SCO audio is
    acquired separately through acquire_audio_stream().

    Recommended outbound-call workflow:
    1. connect_and_wait(address)
    2. dial_and_wait(number)
    3. ensure_audio_stream("active-call")
    4. Use the returned WebSocket for STT/TTS audio.
    5. hangup() when done.
    """
    try:
        normalized = normalize_phone_number(
            number,
            _runtime_config.default_region if _runtime_config else None,
        )
        _reject_self_call(normalized)
        timeout_seconds = validate_timeout(timeout_seconds, maximum=180.0)
    except (ContractError, ValueError, ATCommandError) as exc:
        if isinstance(exc, ContractError):
            return failure(exc.code, exc.message, retryable=exc.retryable)
        return {"ok": False, "error": str(exc)}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    result = await _dial_normalized(
        normalized,
        min(_configured_at_dial_timeout(), timeout_seconds),
    )
    if not result.get("ok"):
        return result

    waited = await _wait_for_status(
        lambda s: s["call_state"] == CallState.ACTIVE.value,
        max(0.0, deadline - loop.time()),
    )
    if not waited.get("ok"):
        intent = dict(result.get("_dial_intent") or {})
        if result.get("confirmation_pending"):
            asyncio.create_task(
                _expire_pending_dial_intent(
                    normalized,
                    intent,
                    _configured_at_dial_timeout(),
                ),
                name="hfp-expire-dial-and-wait-intent",
            )
        else:
            _clear_dial_intent(normalized, intent)
    return waited


@mcp.tool()
async def answer_call(
    call_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    """
    Answer an incoming call (sends ATA to the phone).

    Recommended incoming-call workflow:
    1. get_phone_context() reports incoming_call=true or recommended_next_action=answer_call.
    2. answer_call()
    3. Wait for get_phone_context() to show call_active=true/audio_active=true if needed.
    4. ensure_audio_stream("active-call")
    5. Use the returned WebSocket for STT/TTS audio.
    """
    with _state._lock:
        target_call_id = call_id or _state.call_id

    async def _answer() -> dict:
        global _answered_call_token
        async with _loop_lock(_call_control_locks):
            if _state.connection_state != ConnectionState.CONNECTED:
                return failure(
                    "connection_unavailable",
                    "Phone not connected (HFP not ready)",
                    state=_state.versioned_snapshot(),
                )
            if _state.call_state != CallState.INCOMING:
                return failure(
                    "call_conflict",
                    f"No incoming call to answer (state: {_state.call_state.value})",
                    state=_state.versioned_snapshot(),
                )
            if target_call_id is not None and target_call_id != _state.call_id:
                return failure(
                    "call_conflict",
                    "call_id is not the incoming call",
                    state=_state.versioned_snapshot(),
                )
            active_call_id = _state.call_id
            assert active_call_id is not None
            answer_token = (
                _state.connection_generation,
                _state.call_generation,
                active_call_id,
            )
            if _answered_call_token == answer_token:
                return ok(
                    {
                        "call_id": active_call_id,
                        "answered": True,
                        "already_answered": True,
                    },
                    state=_state.versioned_snapshot(),
                )
            try:
                await get_at_command_broker(_state).execute(CMD_ATA)
            except ATCommandError as exc:
                return failure(
                    "command_rejected",
                    str(exc),
                    state=_state.versioned_snapshot(),
                )
            _answered_call_token = answer_token
            return ok(
                {"call_id": active_call_id, "answered": True},
                state=_state.versioned_snapshot(),
            )

    if request_id:
        try:
            return await _run_idempotent_async(
                request_id,
                f"answer_call:{target_call_id or 'none'}",
                _answer,
            )
        except Exception as exc:
            return _contract_failure(exc)
    response = await _answer()
    return {"ok": True} if response.get("ok") else {"ok": False, "error": response["error"]["message"]}


@mcp.tool()
async def hangup() -> dict:
    """
    End the current call (sends AT+CHUP to the phone).

    Use this after outbound calls, incoming calls, reminders, or live voice
    sessions. Call stop_audio_capture(session_id) if a legacy/base64 capture
    session is still open.
    """
    async with _loop_lock(_call_control_locks):
        if _state.call_state == CallState.IDLE:
            return {"ok": False, "error": "No active call"}
        if _state.call_state == CallState.ENDING:
            return {"ok": True, "already_ending": True}
        call_id = _state.call_id
        call_generation = _state.call_generation
        call_state = _state.call_state
        connection_generation = _state.connection_generation
        try:
            await get_at_command_broker(_state).execute(CMD_CHUP)
        except ATCommandError as exc:
            return {"ok": False, "error": str(exc)}
        transitioned = _state.transition_call_state(
            CallState.ENDING,
            expected_call_id=call_id,
            expected_generation=call_generation,
            allowed_states={call_state},
            expected_connection_generation=connection_generation,
        )
        return {"ok": True, "already_ended": not transitioned}


@mcp.tool()
async def get_call_status() -> dict:
    """
    Return the current Bluetooth connection and call state.

    connection: disconnected | handshaking | connected
    call_state: idle | incoming | dialing | ringing | active | ending
    audio_active: true when the phone reports active call audio
    sco_connected: true when this server has opened the SCO audio bridge

    For agent decision-making, prefer get_phone_context(), which includes
    booleans and recommended_next_action.
    """
    return _state.snapshot()


@mcp.tool()
async def get_phone_context() -> dict:
    """
    Return an agent-friendly summary of the phone/call state with booleans and a
    recommended next action. Prefer this when an LLM needs to choose a call step.

    Typical recommended_next_action values:
    - connect_phone: no phone is connected; use scan_paired_devices then connect_and_wait.
    - wait: connection/call state is changing.
    - dial: phone is connected and idle; use dial_and_wait.
    - answer_call: incoming call is ringing; use answer_call.
    - start_audio_stream: call is active; use ensure_audio_stream.
    - continue_call: call audio is already active/streaming.
    - cleanup_audio_sessions: call is idle but a stale SCO bridge remains.
    """
    status = _state.snapshot()
    connection = status["connection"]
    call_state = status["call_state"]
    connected = connection == ConnectionState.CONNECTED.value
    call_active = call_state == CallState.ACTIVE.value
    incoming = call_state == CallState.INCOMING.value

    stale_audio_session = call_state == CallState.IDLE.value and status["sco_connected"]

    if connection == ConnectionState.DISCONNECTED.value:
        next_action = "connect_phone"
    elif connection == ConnectionState.HANDSHAKING.value:
        next_action = "wait"
    elif stale_audio_session:
        next_action = "cleanup_audio_sessions"
    elif incoming:
        next_action = "answer_call"
    elif call_active and not status["sco_connected"]:
        next_action = "start_audio_stream"
    elif call_active:
        next_action = "continue_call"
    elif call_state in (CallState.DIALING.value, CallState.RINGING.value, CallState.ENDING.value):
        next_action = "wait"
    else:
        next_action = "dial"

    return {
        **status,
        "ready_to_call": connected and call_state == CallState.IDLE.value and not stale_audio_session,
        "connected": connected,
        "incoming_call": incoming,
        "call_active": call_active,
        "recommended_next_action": next_action,
    }


@mcp.tool()
async def start_audio_capture(session_id: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: open the SCO audio link for legacy base64 audio
    chunk tools.

    The call must already be in ACTIVE state (audio_active=true). This connects a
    Bluetooth SCO socket directly (HF-initiated) and streams 8 kHz CVSD PCM — it
    does not depend on PipeWire.

    session_id: arbitrary string to identify this capture session (e.g. "call1").
    Returns ok=true once the SCO bridge is open, or ok=false with an error.

    Legacy/diagnostic tool. Realtime agents should prefer
    ensure_audio_stream(), which returns a duplex WebSocket and avoids large
    repeated MCP base64 tool calls.
    """
    try:
        actual_session_id, _ = _resolve_media_session_id(session_id)
    except ContractError as exc:
        return {"ok": False, "error": exc.message}
    if _audio_manager.get_session(actual_session_id):
        return {"ok": False, "error": f"Session '{actual_session_id}' already exists"}

    result = await _open_audio_session(actual_session_id)
    if not result["ok"]:
        return result

    _state.set_sco_connected(True)
    return {
        "ok": True,
        "transport": "sco",
        "session_id": actual_session_id,
        "mtu": result["mtu"],
    }


async def _start_audio_stream_unlocked(session_id: str) -> dict:
    """
    Start or reuse a call audio session and return a WebSocket stream endpoint.

    The WebSocket is the realtime audio plane. It sends and receives binary PCM:
    8 kHz, signed 16-bit, mono. Use MCP tools for call control; use this stream
    for STT/TTS or live voice agents. The call must already be ACTIVE.

    Prefer ensure_audio_stream("active-call") unless the client needs a custom
    session id. Do not use get_audio_chunk/play_audio for realtime voice unless
    WebSockets are unavailable.
    """
    global _audio_stream_server

    try:
        session_id, lease_reused = _resolve_media_session_id(session_id)
    except ContractError as exc:
        return {"ok": False, "error": exc.message}

    session = _audio_manager.get_session(session_id)
    session_reused = session is not None
    if session is None:
        result = await _open_audio_session(session_id)
        if not result["ok"]:
            return result
        session = _audio_manager.get_session(session_id)
        _state.set_sco_connected(True)
    if session is None:
        return {"ok": False, "error": f"No session '{session_id}'"}
    if _audio_stream_server is None:
        return {"ok": False, "error": "Audio stream sidecar is not configured"}

    lease = _media_leases.current()
    if (
        lease is None
        or lease.stream_id != session_id
        or _state.call_state != CallState.ACTIVE
        or _state.call_id != lease.call_id
    ):
        if _audio_manager.get_session(session_id) is not None:
            if lease is not None and lease.stream_id == session_id:
                _archive_media_metrics(lease, session)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                _audio_manager.remove_session,
                session_id,
            )
            if lease is not None and lease.stream_id == session_id:
                _archive_media_metrics(lease, session)
        return {"ok": False, "error": "Call ended while audio was starting"}

    _audio_stream_server.start()
    token = _audio_stream_server.issue_token(
        session_id,
        call_id=_state.call_id,
        server_instance_id=_state.server_instance_id,
        generation=lease.generation if lease and lease.stream_id == session_id else _state.connection_generation,
        owner=lease.owner if lease and lease.stream_id == session_id else "mcp_client",
    )
    stream_url = _audio_stream_server.stream_url(session_id, token)
    active_lease = _media_leases.current()
    if active_lease is not None and active_lease.stream_id == session_id:
        _state.set_sco_state(
            SCOState.READY,
            bridge_state="ready",
            owner=active_lease.owner,
            stream_id=active_lease.stream_id,
        )
        if _audio_stream_server.client_attached(session_id):
            _cancel_audio_reclaim(session_id)
        else:
            _arm_audio_lease_reclaim(active_lease, TOKEN_TTL_SECONDS)
    return {
        "ok": True,
        "transport": "websocket",
        "stream_url": stream_url,
        "client_stream_url": stream_url,
        "session_id": session_id,
        "mtu": session.mtu,
        "sco_mtu_bytes": session.mtu,
        "session_reused": session_reused,
        "lease_reused": lease_reused,
        "client_attached": _audio_stream_server.client_attached(session_id),
        "token_reissued": True,
        "health": _audio_stream_server.health(),
        "audio": _audio_stream_server.metadata(),
        "direction": "duplex",
        "message_format": "binary PCM frames in both directions",
    }


@mcp.tool()
async def start_audio_stream(session_id: str) -> dict:
    """Start or reuse the active call's authenticated duplex WebSocket."""

    async with _loop_lock(_audio_lifecycle_locks):
        return await _start_audio_stream_unlocked(session_id)


@mcp.tool()
async def ensure_audio_stream(session_id: str = "active-call") -> dict:
    """
    Start or reuse the realtime call audio stream using a stable default session
    id. Prefer this for agents that do not care about naming audio sessions.

    Use after dial_and_wait() for outbound calls or after answer_call() once
    audio_active=true. The returned stream_url is the realtime duplex audio
    channel: receive caller PCM for STT and send TTS PCM for playback.
    """
    return await start_audio_stream(session_id)


@mcp.tool()
async def get_audio_chunk(session_id: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: return the next legacy base64 audio chunk from the
    phone microphone.

    Audio format: 8 kHz, 16-bit signed mono PCM, base64-encoded.
    Returns audio_b64=null if no new audio is available yet (buffer empty).
    Legacy/diagnostic tool. Prefer ensure_audio_stream for realtime STT/TTS.
    """
    session_id = _legacy_stream_aliases.get(session_id, session_id)
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    audio_b64 = session.get_chunk_b64()
    return {"ok": True, "audio_b64": audio_b64}


@mcp.tool()
async def play_audio(session_id: str, audio_b64: str) -> dict:
    """
    DEPRECATED / DIAGNOSTIC: queue legacy base64-encoded PCM audio into the call.

    This does NOT accept an audio file path or compressed audio. audio_b64 must
    be base64-encoded raw PCM bytes: 8 kHz, 16-bit signed, mono.
    Legacy/diagnostic tool. Prefer ensure_audio_stream for realtime TTS/live
    voice agents.

    audio_b64: base64-encoded raw PCM bytes.
    Returns bytes_queued on success.
    """
    session_id = _legacy_stream_aliases.get(session_id, session_id)
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    try:
        loop = asyncio.get_event_loop()
        n = await loop.run_in_executor(None, session.queue_playback_b64, audio_b64)
        return {"ok": True, "bytes_queued": n}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def stop_audio_capture(session_id: str) -> dict:
    """Stop the SCO audio session and free capture/playback resources."""
    async with _loop_lock(_audio_lifecycle_locks):
        requested_id = session_id
        session_id = _legacy_stream_aliases.get(session_id, session_id)
        session = _audio_manager.get_session(session_id)
        if not session:
            return {"ok": False, "error": f"No session '{session_id}'"}
        lease = _media_leases.current()
        if lease is not None and lease.stream_id == session_id:
            _detach_logical_media_lease(lease)
        elif _audio_stream_server is not None:
            _audio_stream_server.detach_session(session_id)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _audio_manager.remove_session, session_id)
        if lease is not None and lease.stream_id == session_id:
            _archive_media_metrics(lease, session)
        _legacy_stream_aliases.pop(requested_id, None)
        _drop_media_aliases(session_id)
        _refresh_audio_state_after_cleanup()
        return {"ok": True}


@mcp.tool()
async def clear_audio_playback(session_id: str = "active-call") -> dict:
    """Drop queued outbound audio for a realtime call audio session."""
    requested_id = session_id
    session_id = _legacy_stream_aliases.get(session_id, session_id)
    session = _audio_manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": f"No session '{session_id}'"}
    loop = asyncio.get_event_loop()
    cleared = await loop.run_in_executor(None, session.clear_playback)
    return {"ok": True, "session_id": requested_id, "bytes_cleared": cleared}


@mcp.tool()
async def cleanup_audio_sessions() -> dict:
    """
    Stop all SCO audio sessions and refresh bridge state.

    This is normally automatic when the phone reports call end. Use it as a
    recovery action if get_phone_context reports cleanup_audio_sessions.
    """
    stopped = await _cleanup_all_audio_sessions()
    return {"ok": True, "sessions_stopped": stopped}


async def _ensure_audio_session(session_id: str, owner: str = "one_shot") -> dict:
    try:
        session_id, lease_reused = _resolve_media_session_id(session_id, owner)
    except ContractError as exc:
        return {"ok": False, "error": exc.message}
    session = _audio_manager.get_session(session_id)
    if session is not None:
        return {
            "ok": True,
            "session": session,
            "session_id": session_id,
            "session_reused": True,
            "lease_reused": lease_reused,
        }

    result = await _open_audio_session(session_id)
    if not result["ok"]:
        return result

    session = _audio_manager.get_session(session_id)
    if session is None:
        return {"ok": False, "error": f"No session '{session_id}'"}
    _state.set_sco_connected(True)
    return {
        "ok": True,
        "session": session,
        "session_id": session_id,
        "session_reused": False,
        "lease_reused": lease_reused,
    }


def _tail_silence(tail_ms: float) -> bytes:
    if tail_ms <= 0:
        return b""
    byte_count = int(AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 * tail_ms / 1000)
    if byte_count % 2:
        byte_count += 1
    return b"\x00" * byte_count


def _convert_audio_file_to_pcm(audio_path: Path) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return proc.stdout


def _allowed_audio_path(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ContractError("invalid_argument", "audio path is not a regular file")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ContractError("invalid_argument", "audio file exceeds the 50 MiB limit")
    roots = _runtime_config.playback_roots if _runtime_config else ()
    if _runtime_config is not None and not roots:
        raise ContractError("permission_denied", "server-local audio playback is disabled")
    if roots and not any(path.is_relative_to(root) for root in roots):
        raise ContractError("permission_denied", "audio file is outside configured playback roots")
    return path


async def _queue_pcm_realtime(session, pcm: bytes) -> int:
    if not pcm:
        return 0

    loop = asyncio.get_event_loop()
    queued = 0
    for offset in range(0, len(pcm), STREAM_FRAME_BYTES):
        frame = pcm[offset:offset + STREAM_FRAME_BYTES]
        await loop.run_in_executor(None, session.queue_playback, frame)
        queued += len(frame)
        await asyncio.sleep(AUDIO_STREAM_FRAME_MS / 1000.0)
    return queued


@mcp.tool()
async def play_audio_file(
    audio_file: str,
    session_id: str = "active-call",
    tail_ms: float = 1000.0,
) -> dict:
    """
    Convert a server-local audio file to HFP PCM and play it into active call
    audio. This is the preferred one-shot playback path for normal MP3/WAV/OGG
    files because MCP owns conversion and paced PCM queueing internally.

    The call must already be active with audio_active=true. Use
    dial_and_play_audio_file() when the tool should place the call first.
    """
    try:
        audio_path = _allowed_audio_path(audio_file)
        if tail_ms < 0 or tail_ms > 5000:
            raise ContractError("invalid_argument", "tail_ms must be between 0 and 5000")
    except FileNotFoundError:
        return {"ok": False, "error": f"Audio file not found: {Path(audio_file).expanduser()}"}
    except ContractError as exc:
        return {"ok": False, "error": exc.message}

    loop = asyncio.get_event_loop()
    try:
        pcm = await loop.run_in_executor(None, _convert_audio_file_to_pcm, audio_path)
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg is not installed or not on PATH"}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        return {
            "ok": False,
            "error": f"ffmpeg conversion failed: {stderr or exc}",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg conversion exceeded 60 seconds"}

    result = await _ensure_audio_session(session_id)
    if not result.get("ok"):
        return result

    payload = pcm + _tail_silence(tail_ms)
    try:
        bytes_queued = await _queue_pcm_realtime(result["session"], payload)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "session_id": session_id,
        "session_reused": bool(result.get("session_reused")),
        "bytes_pcm": len(pcm),
        "tail_ms": tail_ms,
        "bytes_queued": bytes_queued,
    }


@mcp.tool()
async def dial_and_play_audio_file(
    number: str,
    audio_file: str,
    session_id: str | None = None,
    timeout_seconds: float = 30.0,
    hangup_after: bool = False,
    tail_ms: float = 1000.0,
) -> dict:
    """
    Dial a number if needed, then play a server-local audio file over HFP audio.

    If a call is already active and audio_active=true, this attaches to the
    active call instead of failing with "Already in call state: active".
    """
    if _phone_controller and _phone_controller.call_id:
        return {"ok": False, "error": "interactive_call_owns_audio"}
    if _phone_controller:
        _phone_controller.suspended += 1
    try:
        playback_session_id = session_id or f"hfp-call-{uuid.uuid4()}"
        dialed = False

        if _state.call_state == CallState.IDLE:
            result = await dial_and_wait(number, timeout_seconds)
            if not result.get("ok"):
                return result
            dialed = True
        elif not (_state.call_state == CallState.ACTIVE and _state.audio_active):
            return {
                "ok": False,
                "error": f"Already in call state: {_state.call_state.value}",
            }

        if _phone_controller:
            _phone_controller.excluded_call_id = _state.call_id
        result = await play_audio_file(audio_file, playback_session_id, tail_ms)
        if not result.get("ok"):
            if dialed:
                await hangup()
                await cleanup_audio_sessions()
            return {**result, "dialed": dialed}

        if hangup_after:
            await hangup()

        return {**result, "dialed": dialed}
    finally:
        if _phone_controller:
            _phone_controller.suspended -= 1



def _live_call_context() -> dict:
    status = _state.snapshot()
    return {
        "call_state": status["call_state"],
        "call_active": status["call_state"] == CallState.ACTIVE.value,
        "audio_active": bool(status["audio_active"]),
    }


async def _acquire_gemini_stream(_session_id: str) -> dict:
    if _state.call_state != CallState.ACTIVE or not _state.call_id:
        return {"ok": False, "error": "no_active_call"}
    response = await acquire_audio_stream(
        _state.call_id,
        "gemini_live",
        f"gemini-audio-{_state.call_id}-{uuid.uuid4().hex}",
    )
    if not response.get("ok"):
        error = response.get("error") or {}
        return {
            "ok": False,
            "error": error.get("message", "audio_stream_failed")
            if isinstance(error, dict)
            else str(error),
        }
    result = dict(response.get("result") or {})
    stream_url = result.get("client_stream_url") or result.get("stream_url")
    return {
        "ok": True,
        **result,
        "client_stream_url": stream_url,
        "stream_url": stream_url,
    }


async def _clear_gemini_playback(_session_id: str) -> dict:
    lease = _media_leases.current()
    if lease is None or lease.owner != "gemini_live":
        return {"ok": False, "error": "gemini_audio_lease_missing"}
    return await clear_audio_playback(lease.stream_id)


def _new_live_ai_manager(allowed_tools: frozenset[str]) -> GeminiLiveManager:
    return GeminiLiveManager(
        ensure_stream=_acquire_gemini_stream,
        clear_playback=_clear_gemini_playback,
        hangup=hangup,
        release_stream=release_audio_stream,
        allowed_tools=allowed_tools,
        full_transcripts_enabled=bool(
            _runtime_config and _runtime_config.full_transcripts
        ),
        transcript_sink=_persist_transcript,
    )


def _get_live_ai_manager() -> GeminiLiveManager:
    """Return the current manager without changing its caller tool policy.

    Status, polling, result submission, and cleanup calls must keep observing
    the same manager even after it stops or fails. Replacing it here based on a
    default tool policy would discard its error, transcript summary, and
    unresolved request state merely because a client performed a read.
    """

    global _gemini_live_manager, _gemini_allowed_tools
    if _gemini_live_manager is None:
        desired = frozenset(HERMES_TOOL_NAMES)
        _gemini_live_manager = _new_live_ai_manager(desired)
        _gemini_allowed_tools = desired
    return _gemini_live_manager


def _prepare_live_ai_manager(allowed_tools: frozenset[str]) -> GeminiLiveManager:
    """Apply caller policy only while explicitly setting up a new Live call."""

    global _gemini_live_manager, _gemini_allowed_tools
    desired = frozenset(allowed_tools)
    if _gemini_live_manager is None or (
        _gemini_allowed_tools != desired and not _gemini_live_manager.running
    ):
        _gemini_live_manager = _new_live_ai_manager(desired)
        _gemini_allowed_tools = desired
    return _gemini_live_manager


def _get_gemini_live_manager() -> GeminiLiveManager:
    return _get_live_ai_manager()


@mcp.tool()
async def start_live_ai_call(
    session_id: str = "active-call",
    initial_context: str | None = None,
    caller_role: str = "mcp_client",
) -> dict:
    """
    Start the configured provider-neutral Live AI call session.

    Gemini is currently the only provider implementation. The generic tool name
    is the stable MCP contract for future providers. ``initial_context`` is an
    internal instruction, not literal caller-facing speech. On a fresh session
    it is installed in setup; if the Hermes phone platform already auto-started
    this same physical call, it is delivered through realtime text instead of
    being discarded. Inspect ``initial_context_applied`` in the result. For an
    exact caller-facing sentence use ``speak_to_caller`` after this tool; for a
    modeled action such as "immediately ask the caller ...", use
    ``send_live_instruction`` after this tool.
    """
    if _state.call_state != CallState.ACTIVE or not _state.call_id:
        return failure(
            "call_conflict",
            "Gemini Live requires an active phone call",
            state=_state.versioned_snapshot(),
        )
    if _phone_controller is not None:
        return failure("phone_controller_owns_voice", "Use configured phone routes; voice is managed by the controller")
    requested_role = str(caller_role or "mcp_client").strip().lower()
    if requested_role not in {"admin", "trusted", "unknown", "blocked", "mcp_client"}:
        return failure("invalid_argument", "unsupported caller_role")
    # ``caller_role`` remains in the public signature for compatibility, but it
    # is never an authorization input. Only the daemon's normalized HFP identity
    # classification may select the session-scoped Gemini tool declarations.
    with _state._lock:
        role = _state.caller_role
    if role not in {"admin", "trusted", "unknown", "blocked"}:
        role = "unknown"
    allowed_tools = (
        frozenset()
        if role in {"unknown", "blocked"}
        else frozenset(HERMES_TOOL_NAMES)
    )
    if (
        _gemini_live_manager is not None
        and getattr(_gemini_live_manager, "running", False)
        and _gemini_allowed_tools != allowed_tools
    ):
        return failure(
            "live_ai_policy_conflict",
            "running Live AI session has a different caller tool policy; stop it before restarting",
            state=_state.versioned_snapshot(),
        )
    # The physical call ID is the only session identity allowed to own SCO.
    result = await _prepare_live_ai_manager(allowed_tools).start(
        _state.call_id,
        initial_context,
    )
    _sync_live_ai_state()
    return result


@mcp.tool()
async def stop_live_ai_call(
    reason: str | None = None,
    hangup_after: bool = False,
) -> dict:
    """Stop the active Live AI session, optionally hanging up the phone call."""
    result = await _get_live_ai_manager().stop(reason, hangup_after)
    _sync_live_ai_state()
    return result


@mcp.tool()
async def get_live_ai_status() -> dict:
    """Return provider availability, request counts, and call/session coupling."""
    result = _get_live_ai_manager().status(_live_call_context())
    with _state._lock:
        result["caller_role"] = _state.caller_role
        result["remote_number_verified"] = bool(
            _state.remote_number
            and _state.remote_number_source in {"clip", "clcc", "dialed"}
        )
    _sync_live_ai_state()
    metrics = _transport_metrics_for_call(result.get("session_id"))
    result["transport_metrics"] = dict(metrics) if metrics is not None else None
    return result


@mcp.tool()
async def send_live_instruction(text: str, urgency: str = "normal") -> dict:
    """Send internal context to the live assistant without speaking it verbatim."""
    return await _get_live_ai_manager().send_text(
        text,
        urgency=urgency,
        speak_to_caller=False,
    )


@mcp.tool()
async def speak_to_caller(text: str, urgency: str = "normal") -> dict:
    """Send text that should be spoken to the caller by the live assistant."""
    return await _get_live_ai_manager().speak(text, urgency=urgency)


@mcp.tool()
async def send_live_ai_text(
    text: str,
    urgency: str = "normal",
    speak_to_caller: bool = False,
) -> dict:
    """Send text to the active Live AI session; defaults to internal context."""
    return await _get_live_ai_manager().send_text(
        text,
        urgency=urgency,
        speak_to_caller=speak_to_caller,
    )


@mcp.tool()
async def poll_live_ai_requests(timeout_seconds: float = 5.0) -> dict:
    """Poll provider function calls waiting for MCP-client/tool orchestration."""
    return await _get_live_ai_manager().poll_requests(timeout_seconds)


@mcp.tool()
def get_live_ai_pending_requests() -> dict:
    """Return function calls already polled but not yet answered."""
    return _get_live_ai_manager().pending_requests()


@mcp.tool()
async def submit_live_ai_result(
    request_id: str,
    result: str,
    speak_to_caller: bool = True,
) -> dict:
    """Return an MCP client/tool result to the active Live AI provider."""
    return await _get_live_ai_manager().submit_result(
        request_id,
        result,
        speak_to_caller=speak_to_caller,
    )


@mcp.tool()
def cancel_live_request(request_id: str, reason: str = "cancelled") -> dict:
    """Mark one unresolved Live AI request stale/cancelled."""
    return _get_live_ai_manager().cancel_request(request_id, reason)


@mcp.tool()
def clear_live_requests(
    session_id: str | None = None,
    only_stale: bool = True,
) -> dict:
    """Clear stale/cancelled Live AI requests, or all unresolved requests."""
    return _get_live_ai_manager().clear_requests(
        session_id,
        only_stale=only_stale,
    )


@mcp.tool()
def get_call_transcript(session_id: str | None = None, after_id: int = 0, limit: int = 500) -> dict:
    """Return transcript events only when the operator explicitly enabled them."""
    if _runtime_config is None or not _runtime_config.full_transcripts:
        return failure(
            "privacy_disabled",
            "Full transcript access is disabled; enable HFP_FULL_TRANSCRIPTS explicitly",
        )
    if _request_ledger is not None:
        return _request_ledger.transcript(session_id or _state.call_id, after_id=after_id, limit=limit)
    return _get_live_ai_manager().get_call_transcript(session_id)


@mcp.tool()
def list_call_transcripts(limit: int = 50) -> dict:
    """List retained call transcript IDs, newest first. Requires explicit opt-in."""
    if _runtime_config is None or not _runtime_config.full_transcripts:
        return failure("privacy_disabled", "Full transcript access is disabled")
    return {"ok": True, "calls": _request_ledger.transcript_calls(limit) if _request_ledger else []}


def _persist_transcript(event):
    if _runtime_config and _runtime_config.full_transcripts and _request_ledger:
        _request_ledger.append_transcript(event)


@mcp.tool()
def get_last_call_summary(session_id: str | None = None) -> dict:
    """Return the retained redacted summary without exposing transcript text."""
    target = session_id
    if target is None and _gemini_live_manager is not None:
        target = _gemini_live_manager.status().get("session_id")
    transport_metrics = _transport_metrics_for_call(target)
    if target and _request_ledger is not None:
        persisted = _request_ledger.get_call_summary(target)
        if persisted is not None:
            response = ok(persisted)
            response["transport_metrics"] = transport_metrics
            return response
    summary = _get_live_ai_manager().get_last_call_summary(target)
    if _runtime_config is None or not _runtime_config.full_transcripts:
        summary.pop("last_input_transcript", None)
        summary.pop("last_output_transcript", None)
        summary["transcript_text_retained"] = False
    summary["transport_metrics"] = transport_metrics
    return summary


def _register_gemini_live_tools() -> bool:
    availability = gemini_live_availability()
    if not availability.get("available"):
        log.info("Gemini Live MCP tools disabled: %s", availability.get("reason"))
        return False

    @mcp.tool()
    async def start_gemini_live_call(
        session_id: str = "active-call",
        initial_context: str | None = None,
        caller_role: str = "mcp_client",
    ) -> dict:
        """
        Start Gemini Live for the active call.

        Gemini owns the realtime HFP audio WebSocket while this session runs.
        MCP clients or orchestrators should exchange text/instructions through
        the Gemini tools instead of opening the audio stream directly. Late
        ``initial_context`` for an already-running physical call is applied as
        realtime internal context and reported in the result; use
        ``speak_to_caller`` when exact immediate speech is required.
        """
        return await start_live_ai_call(session_id, initial_context, caller_role)

    @mcp.tool()
    async def stop_gemini_live_call(
        reason: str | None = None,
        hangup_after: bool = False,
    ) -> dict:
        """Stop the active Gemini Live call session, optionally hanging up."""
        return await stop_live_ai_call(reason, hangup_after)

    @mcp.tool()
    async def get_gemini_live_status() -> dict:
        """Return Gemini Live availability and active-session status."""
        return await get_live_ai_status()

    @mcp.tool()
    async def send_gemini_live_text(
        text: str,
        urgency: str = "normal",
        speak_to_caller: bool = True,
    ) -> dict:
        """Compatibility alias for send_live_ai_text.

        This keeps the historical speak_to_caller=true default for existing
        Gemini clients. New clients should use send_live_instruction() or
        speak_to_caller() for safer intent-specific behavior.
        """
        return await send_live_ai_text(
            text,
            urgency=urgency,
            speak_to_caller=speak_to_caller,
        )

    @mcp.tool()
    async def poll_gemini_live_requests(timeout_seconds: float = 5.0) -> dict:
        """Poll Gemini function calls waiting for MCP-client/tool orchestration."""
        return await poll_live_ai_requests(timeout_seconds)

    @mcp.tool()
    def get_gemini_live_pending_requests() -> dict:
        """
        Return Gemini function calls already polled but not yet answered.

        Generic MCP clients should use this as a recovery/introspection tool:
        poll_gemini_live_requests() reserves requests for later
        submit_gemini_live_result(), and this tool exposes those reserved
        request IDs if a client disconnects or needs to resume orchestration.
        """
        return get_live_ai_pending_requests()

    @mcp.tool()
    async def submit_gemini_live_result(
        request_id: str,
        result: str,
        speak_to_caller: bool = True,
    ) -> dict:
        """Return an MCP client/tool result to Gemini for a pending function call."""
        return await submit_live_ai_result(
            request_id,
            result,
            speak_to_caller=speak_to_caller,
        )

    return True


GEMINI_LIVE_TOOLS_REGISTERED = _register_gemini_live_tools()


async def _open_audio_session(session_id: str) -> dict:
    if _state.call_state != CallState.ACTIVE:
        return {
            "ok": False,
            "error": "No active call — wait for call_state=active",
        }

    address = _state.connected_address
    if not address:
        return {"ok": False, "error": "No connected phone address"}

    loop = asyncio.get_event_loop()
    try:
        session = _audio_manager.create_session(session_id, address)
        _state.set_sco_state(SCOState.CONNECTING, bridge_state="connecting")
        await loop.run_in_executor(None, session.start)
    except (SCOAudioError, ValueError) as exc:
        _audio_manager.remove_session(session_id)
        _state.set_sco_state(SCOState.FAILED, bridge_state="failed")
        return {
            "ok": False,
            "error": (
                f"{exc}. Confirm the call is still active and that the Bluetooth "
                "controller routes SCO over HCI (hciconfig hci0 should show SCO "
                "RX/TX counters during a call)."
            ),
        }
    _state.set_sco_state(SCOState.READY, bridge_state="ready")
    _state.set_audio_active(True)
    return {"ok": True, "transport": "sco", "mtu": session.mtu}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _create_phone_controller(routing):
    from .phone_controller import PhoneController
    from .voice_backends import ClassicVoice, GeminiVoice

    async def end(call_id):
        return await end_call(call_id, f"phone-end-{call_id}", wait=False)

    async def answer(call_id):
        result = await answer_call(call_id, f"phone-answer-{call_id}")
        if not result.get("ok"):
            raise RuntimeError("could not answer routed call")

    async def acquire_classic(call_id):
        return await acquire_audio_stream(call_id, "hermes_classic", f"phone-audio-{uuid.uuid4().hex}")

    async def connect():
        if _runtime_config and _runtime_config.device_address:
            await ensure_phone_connected(_runtime_config.device_address, f"phone-connect-{uuid.uuid4().hex}")

    def voice(mode, controller):
        global _gemini_live_manager, _gemini_allowed_tools
        if mode == "classic":
            return ClassicVoice(controller, acquire=acquire_classic,
                release=release_audio_stream, clear=clear_audio_playback)
        _gemini_allowed_tools = frozenset({"ask_hermes", "end_call", "phone_status"})
        if controller.conversation:
            _gemini_allowed_tools |= {"phone_recall", "phone_session", "hermes_task"}
        routed_call_id = controller.call_id
        async def routed_hangup():
            result = await end(routed_call_id)
            if not result.get("ok"):
                raise RuntimeError("phone rejected hangup")
            return result
        _gemini_live_manager = GeminiLiveManager(
            ensure_stream=_acquire_gemini_stream, clear_playback=_clear_gemini_playback,
            hangup=routed_hangup, release_stream=release_audio_stream,
            allowed_tools=_gemini_allowed_tools, request_handler=controller.delegate,
            full_transcripts_enabled=bool(controller.conversation or (_runtime_config and _runtime_config.full_transcripts)),
            transcript_sink=controller.conversation.capture if controller.conversation else _persist_transcript,
            context_provider=controller.conversation.context if controller.conversation else None,
            context_ready=controller.conversation.voice_ready if controller.conversation else None)
        return GeminiVoice(_gemini_live_manager)

    def timing(call_id, stage, detail):
        if _request_ledger:
            _request_ledger.audit("phone_timing", stage, call_id=call_id, detail=detail)

    return PhoneController(routing, snapshot=_state.versioned_snapshot,
                           answer=answer, end=end, make_voice=voice, connect=connect,
                           transcript_sink=_persist_transcript, timing_sink=timing, ledger=_request_ledger)


@mcp.tool()
async def start_phone_call(number: str, request_id: str) -> dict:
    """Start a routed interactive call; success requires the voice backend ready."""
    if _phone_controller is None:
        return failure("phone_routing_disabled", "Configure phone routes before starting an interactive call")
    route, reason = _phone_controller.config.resolve(number)
    if route is None:
        return failure("phone_route_unavailable", reason)
    result = await place_call(number, request_id)
    if not result.get("ok"):
        return result
    call_id = _state.call_id
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and _state.call_id == call_id:
        status = _phone_controller.status
        if status.get("call_id") == call_id:
            if status.get("state") == "ready":
                return ok({"call_id": call_id, "voice": status.get("voice"), "profile": status.get("profile")})
            if status.get("state") == "failed":
                return failure("phone_voice_failed", "Call voice failed; do not redial automatically")
        await asyncio.sleep(0.1)
    return failure("phone_voice_timeout", "Call voice did not become ready; inspect call state before retrying")


def create_http_app(
    config: RuntimeConfig,
    bearer_token: str,
    *,
    start_bluetooth: bool = True,
):
    """Build the unified authenticated MCP/state/audio ASGI application."""
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.resolved_allowed_hosts()),
        allowed_origins=list(config.resolved_allowed_origins()),
    )
    app = mcp.streamable_http_app()

    from .http_routes import HttpRuntime, control_routes
    runtime = HttpRuntime(
        state=_state, sync_state=_sync_live_ai_state,
        controller=lambda: _phone_controller, ledger=lambda: _request_ledger,
        audio_server=lambda: _audio_stream_server,
        transcript=get_call_transcript, transcripts=list_call_transcripts,
        start_call=start_phone_call,
    )
    app.router.routes.extend(control_routes(config, runtime))
    app.add_middleware(
        BearerAuthMiddleware,
        token=bearer_token,
        public_paths=("/healthz", "/readyz"),
    )
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=config.resolved_allowed_hosts(),
        allowed_origins=config.resolved_allowed_origins(),
    )
    session_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _app_lifespan(starlette_app):
        global _phone_controller
        from .routing import RoutingConfig
        routing = RoutingConfig.load() if start_bluetooth else RoutingConfig()
        if start_bluetooth:
            await _start_bluetooth_stack()
        _state.set_health("http", "ok")
        if start_bluetooth and routing.enabled:
            _phone_controller = _create_phone_controller(routing)
            _phone_controller.start()
        try:
            async with session_lifespan(starlette_app):
                yield
        finally:
            if _phone_controller:
                await _phone_controller.close()
                _phone_controller = None
            _state.set_health("http", "stopped")
            if start_bluetooth:
                _stop_bluetooth_stack()

    app.router.lifespan_context = _app_lifespan
    return app

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HFP MCP Server — Bluetooth phone calling for Linux/Raspberry Pi"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help=(
            "MCP transport: 'stdio' (default) for same-machine clients; "
            "'streamable-http' to serve over HTTP so remote machines can connect"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host for streamable-http transport (secure default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the streamable-http MCP transport (default: 8000)",
    )
    parser.add_argument(
        "--status-port",
        type=int,
        default=None,
        help="Deprecated; /v1/state and /status now share the MCP port",
    )
    parser.add_argument(
        "--audio-host",
        default=None,
        help="Deprecated in HTTP mode; audio now shares the authenticated control service",
    )
    parser.add_argument(
        "--audio-port",
        type=int,
        default=None,
        help="Deprecated in HTTP mode; audio now shares the MCP port",
    )
    parser.add_argument(
        "--audio-public-host",
        default=None,
        help=(
            "Host/IP to place in returned audio WebSocket URLs. Useful when "
            "--audio-host is 0.0.0.0 for remote LAN clients."
        ),
    )
    parser.add_argument(
        "--public-base-url",
        default=None,
        help="External HTTPS origin when a loopback daemon is behind a trusted TLS proxy",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        metavar="HOST[:PORT]",
        default=None,
        help=(
            "Add a Host header value the HTTP transport will accept (repeatable). "
            "Use a 'host:*' pattern to allow any port, e.g. 'raspberrypi.local:*'. "
            "DNS-rebinding protection is always enabled."
        ),
    )
    parser.add_argument("--allowed-origin", action="append", default=None)
    parser.add_argument("--bearer-token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument("--trusted-tls-proxy", action="store_true", default=None)
    parser.add_argument("--adapter-address", default=None)
    parser.add_argument("--device-address", default=None)
    parser.add_argument("--default-region", default=None)
    parser.add_argument("--config", default=None, help="YAML configuration file")
    parser.add_argument(
        "--direct-owner",
        action="store_true",
        help="Development only: let stdio mode own BlueZ instead of proxying to the daemon",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    env = dict(os.environ)
    if args.config:
        env["HFP_MCP_CONFIG"] = args.config
        os.environ["HFP_MCP_CONFIG"] = args.config

    if args.transport == "stdio" and not args.direct_owner:
        from .stdio_proxy import main as run_stdio_proxy

        proxy_config = RuntimeConfig.load(environ=env, include_service_env=True)
        proxy_url = proxy_config.daemon_url or "http://127.0.0.1:8000/mcp"
        proxy_token = (
            args.bearer_token
            or env.get("HFP_PHONE_MCP_TOKEN")
            or proxy_config.bearer_token
        )
        if not proxy_token:
            proxy_token = load_or_create_token(proxy_config.bearer_token_file)
        run_stdio_proxy(proxy_url, proxy_token)
        return

    overrides = {
        "host": args.host,
        "port": args.port,
        "public_host": args.audio_public_host,
        "public_base_url": args.public_base_url,
        "bearer_token": args.bearer_token,
        "tls_cert": Path(args.tls_cert).expanduser() if args.tls_cert else None,
        "tls_key": Path(args.tls_key).expanduser() if args.tls_key else None,
        "trusted_tls_proxy": args.trusted_tls_proxy,
        "allowed_hosts": tuple(args.allowed_host) if args.allowed_host else None,
        "allowed_origins": tuple(args.allowed_origin) if args.allowed_origin else None,
        "adapter_address": args.adapter_address,
        "device_address": args.device_address,
        "default_region": args.default_region,
    }
    config = RuntimeConfig.load(
        environ=env,
        overrides=overrides,
        include_service_env=True,
    )
    if not config.device_address:
        log.warning(
            "No HFP_PHONE_ADDRESS configured: daemon is locked to bounded enrollment mode"
        )

    global _runtime_config, _request_ledger, _control_token, STATE_FILE
    _runtime_config = config
    STATE_FILE = config.state_file
    _request_ledger = RequestLedger(config.database_file, config.retention_days)

    global _audio_stream_server

    if args.transport == "streamable-http":
        global _http_mode
        _http_mode = True
        _control_token = load_or_create_token(
            config.bearer_token_file,
            config.bearer_token,
        )
        _audio_stream_server = AudioStreamServer(
            _audio_manager,
            config.host,
            config.port,
            config.public_host,
            secure=bool(config.tls_cert or config.trusted_tls_proxy),
            embedded=True,
            grant_validator=_audio_grant_is_current,
            public_base_url=config.public_base_url,
            on_client_connected=_on_audio_client_connected,
            on_client_disconnected=_on_audio_client_disconnected,
        )

        # FastMCP.run() does not accept host/port — they must be set on the
        # settings object before run() (see hfp_mcp/transport.py).
        apply_network_settings(mcp.settings, config.host, config.port)

        log.info(
            "MCP endpoint:    %s://%s:%d/mcp",
            "https" if config.tls_cert or config.trusted_tls_proxy else "http",
            config.host,
            config.port,
        )
        log.info(
            "State endpoint:  /v1/state (Bearer auth required); token file: %s",
            config.bearer_token_file,
        )
        if args.status_port or args.audio_host or args.audio_port:
            log.warning("Legacy status/audio port flags are ignored in unified HTTP mode")

        # The FastMCP HTTP app's own lifespan only runs the MCP session
        # manager — our Bluetooth stack lives on the per-session server, which
        # would leave BlueZ uninitialised until a client connects. Wrap the app
        # lifespan so the Bluetooth stack comes up at process startup instead.
        import uvicorn
        app = create_http_app(config, _control_token)
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            ssl_certfile=str(config.tls_cert) if config.tls_cert else None,
            ssl_keyfile=str(config.tls_key) if config.tls_key else None,
            proxy_headers=config.trusted_tls_proxy,
        )
    else:
        _audio_stream_server = AudioStreamServer(
            _audio_manager,
            args.audio_host or AUDIO_STREAM_HOST,
            args.audio_port or AUDIO_STREAM_PORT,
            args.audio_public_host,
            grant_validator=_audio_grant_is_current,
            on_client_connected=_on_audio_client_connected,
            on_client_disconnected=_on_audio_client_disconnected,
        )
        mcp.run("stdio")

    if _request_ledger is not None:
        _request_ledger.close()
