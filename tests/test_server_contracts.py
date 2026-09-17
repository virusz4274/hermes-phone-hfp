import asyncio
from pathlib import Path

from hfp_mcp import server
from hfp_mcp.audio.sco import AudioManager
from hfp_mcp.contracts import RequestLedger
from hfp_mcp.media import MediaLeaseManager
from hfp_mcp.hfp.session import ATCommandError, ATCommandTimeout
from hfp_mcp.settings import RuntimeConfig
from hfp_mcp.state import CallDirection, CallState, ConnectionState, HFPState


async def test_call_end_cleanup_reclaims_old_lease_without_touching_new_call(
    monkeypatch,
):
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    old_call_id = state.call_id
    old_generation = state.call_generation
    leases = MediaLeaseManager()
    old_lease, _ = leases.acquire(old_call_id, "mcp_client")
    manager = AudioManager()
    manager.create_session(old_lease.stream_id, "AA:BB:CC:DD:EE:FF")

    class Sidecar:
        def __init__(self):
            self.detached = []

        def detach_session(self, stream_id):
            self.detached.append(stream_id)

        def client_attached(self, _stream_id):
            return False

    sidecar = Sidecar()

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_manager", manager)
    monkeypatch.setattr(server, "_audio_stream_server", sidecar)
    monkeypatch.setattr(server, "_gemini_live_manager", None)
    monkeypatch.setattr(server, "_audio_lifecycle_locks", {})
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "_legacy_stream_aliases", {"active-call": old_lease.stream_id})

    state.set_call_state(CallState.IDLE)
    server._schedule_call_end_audio_cleanup(
        state.connection_generation,
        old_generation,
    )
    assert leases.current() is None

    state.set_call_state(CallState.INCOMING)
    new_lease, _ = leases.acquire(state.call_id, "mcp_client")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert state.call_generation > old_generation
    assert leases.current() == new_lease
    assert manager.get_session(old_lease.stream_id) is None
    assert sidecar.detached == [old_lease.stream_id]


async def test_call_end_signals_live_ai_before_audio_websocket_detaches(monkeypatch):
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    call_id = state.call_id
    call_generation = state.call_generation
    leases = MediaLeaseManager()
    lease, _ = leases.acquire(call_id, "gemini_live")
    audio = AudioManager()
    audio.create_session(lease.stream_id, "AA:BB:CC:DD:EE:FF")
    events = []

    class Sidecar:
        def detach_session(self, stream_id):
            events.append(("detach", stream_id))

    class LiveManager:
        def status(self):
            return {"session_id": call_id, "running": True}

        def signal_stop(self, reason):
            events.append(("signal", reason))

        async def stop(self, reason, hangup):
            events.append(("stop", reason, hangup))

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_manager", audio)
    monkeypatch.setattr(server, "_audio_stream_server", Sidecar())
    monkeypatch.setattr(server, "_gemini_live_manager", LiveManager())
    monkeypatch.setattr(server, "_audio_lifecycle_locks", {})
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "_legacy_stream_aliases", {})

    state.set_call_state(CallState.IDLE)
    server._schedule_call_end_audio_cleanup(
        state.connection_generation,
        call_generation,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert events[0] == ("signal", "call_ended")
    assert events[1] == ("detach", lease.stream_id)
    assert events[2] == ("stop", "call_ended", False)


async def test_concurrent_dial_sends_only_one_at_command(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.connected_address = "AA:BB:CC:DD:EE:FF"
    entered = asyncio.Event()
    release = asyncio.Event()
    commands = []

    class Broker:
        async def execute(self, command, **_kwargs):
            commands.append(command)
            entered.set()
            await release.wait()

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_runtime_config", None)
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    first = asyncio.create_task(server.dial("+14155552671"))
    await entered.wait()
    second = asyncio.create_task(server.dial("+14155552672"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert len(commands) == 1
    assert sum(bool(result["ok"]) for result in results) == 1
    assert state.call_state == CallState.DIALING


async def test_dial_rejects_configured_handset_number_before_at_command(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED

    class Broker:
        async def execute(self, _command, **_kwargs):
            raise AssertionError("self-call must be rejected before ATD")

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(
        server,
        "_runtime_config",
        RuntimeConfig(default_region="IN", self_number="+919074972348"),
    )
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    local = await server.dial("9074972348")
    international = await server.dial("+919074972348")
    idempotent = await server.place_call(
        "9074972348", "self-call-regression", timeout_seconds=1
    )

    assert local["ok"] is False
    assert international["ok"] is False
    assert idempotent["ok"] is False
    assert local["error"]["code"] == "self_call_blocked"
    assert international["error"]["code"] == "self_call_blocked"
    assert idempotent["error"]["code"] == "self_call_blocked"
    assert state.call_state == CallState.IDLE


async def test_dial_terminal_ok_cannot_regress_active_urc(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED

    class Broker:
        async def execute(self, _command, **_kwargs):
            state.set_call_state(CallState.ACTIVE)

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_runtime_config", None)
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    result = await server.dial("+14155552671")

    assert result["ok"] is True
    assert state.call_state == CallState.ACTIVE
    assert state.remote_number == "+14155552671"


async def test_place_call_reconciles_ambiguous_atd_timeout_without_redial(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    commands = []

    class Broker:
        async def execute(self, command, *, timeout):
            commands.append((command, timeout))

            async def late_progress():
                await asyncio.sleep(0.01)
                state.set_call_state(
                    CallState.ACTIVE,
                    direction=CallDirection.OUTGOING,
                )

            asyncio.create_task(late_progress())
            raise ATCommandTimeout(command, "timeout")

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(
        server,
        "_runtime_config",
        RuntimeConfig(
            at_dial_timeout_seconds=1.0,
            admin_callers=("+14155552671",),
        ),
    )
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    result = await server.place_call(
        "+14155552671",
        "late-atd-timeout",
        timeout_seconds=1.0,
    )

    assert result["ok"] is True
    assert len(commands) == 1
    assert commands[0][1] == 1.0
    assert state.remote_number == "+14155552671"
    assert state.caller_role == "admin"


async def test_explicit_atd_error_clears_pending_identity(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED

    class Broker:
        async def execute(self, command, **_kwargs):
            raise ATCommandError(command, "ERROR")

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_runtime_config", RuntimeConfig())
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    result = await server.dial("+14155552671")

    assert result["ok"] is False
    assert state.call_state == CallState.IDLE
    assert state.remote_number is None
    assert state.caller_role == "unknown"


async def test_cancelled_ambiguous_dial_sends_chup_after_quarantine(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    commands = []

    class Broker:
        terminal_quarantined = False

        async def execute(self, command, **_kwargs):
            if command.startswith("ATD"):
                commands.append(command)
                self.terminal_quarantined = True

                async def phone_progress_and_drain():
                    await asyncio.sleep(0.005)
                    state.set_call_state(
                        CallState.DIALING,
                        direction=CallDirection.OUTGOING,
                    )
                    await asyncio.sleep(0.08)
                    self.terminal_quarantined = False

                asyncio.create_task(phone_progress_and_drain())
                raise ATCommandTimeout(command, "timeout")
            if self.terminal_quarantined:
                raise ATCommandError(
                    command,
                    "previous AT command terminal response unresolved",
                )
            commands.append(command)
            state.set_call_state(CallState.IDLE)

    broker = Broker()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(
        server,
        "_runtime_config",
        RuntimeConfig(at_dial_timeout_seconds=1.0),
    )
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: broker)

    result = await server.place_call(
        "+14155552671",
        "cancel-after-quarantine",
        timeout_seconds=0.03,
    )
    await asyncio.sleep(0.2)

    assert result["ok"] is False
    assert result["error"]["code"] == "timeout_cancel_pending"
    assert sum(command.startswith("ATD") for command in commands) == 1
    assert commands.count("AT+CHUP\r") == 1
    assert state.call_state == CallState.IDLE


async def test_hangup_terminal_ok_cannot_recreate_call_ended_by_urc(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    call_generation = state.call_generation

    class Broker:
        async def execute(self, _command):
            state.set_call_state(CallState.IDLE)

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: Broker())

    result = await server.hangup()

    assert result == {"ok": True, "already_ended": True}
    assert state.call_state == CallState.IDLE
    assert state.call_id is None
    assert state.call_generation == call_generation


async def test_concurrent_answer_sends_only_one_ata(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.INCOMING)
    entered = asyncio.Event()
    release = asyncio.Event()
    commands = []

    class Broker:
        async def execute(self, command):
            commands.append(command)
            entered.set()
            await release.wait()

    broker = Broker()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_call_control_locks", {})
    monkeypatch.setattr(server, "_answered_call_token", None)
    monkeypatch.setattr(server, "get_at_command_broker", lambda _state: broker)

    first = asyncio.create_task(server.answer_call())
    await entered.wait()
    second = asyncio.create_task(server.answer_call())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert results == [{"ok": True}, {"ok": True}]
    assert len(commands) == 1


async def test_connect_timeout_resets_state_and_allows_retry(monkeypatch):
    address = "AA:BB:CC:DD:EE:FF"
    state = HFPState()
    calls = []

    class Manager:
        def connect_device(self, target):
            calls.append(("connect", target))

        def disconnect_device(self, target):
            calls.append(("disconnect", target))

    async def immediate_timeout(_predicate, timeout_seconds, interval=0.25):
        return {
            "ok": False,
            "error": f"Timed out after {timeout_seconds:g}s",
            "status": state.snapshot(),
        }

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_manager", Manager())
    monkeypatch.setattr(server, "_runtime_config", RuntimeConfig(device_address=address))
    monkeypatch.setattr(server, "_connection_context", None)
    monkeypatch.setattr(server, "_profile_ref", None)
    monkeypatch.setattr(server, "_connection_control_locks", {})
    monkeypatch.setattr(server, "_wait_for_status", immediate_timeout)

    first = await server.connect_and_wait(address, 1.0)
    second = await server.connect_and_wait(address, 1.0)

    assert first["ok"] is False and first["retryable"] is True
    assert second["ok"] is False and second["retryable"] is True
    assert state.connection_state == ConnectionState.DISCONNECTED
    assert calls.count(("connect", address)) == 2


async def test_hfp_handshake_wins_over_incomplete_bluez_connect_reply(monkeypatch):
    address = "AA:BB:CC:DD:EE:FF"
    state = HFPState()

    class Manager:
        def connect_device(self, target):
            assert target == address
            with state._lock:
                state.connected_address = target
                state.connection_state = ConnectionState.CONNECTED
            raise RuntimeError("org.freedesktop.DBus.Error.NoReply")

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_manager", Manager())
    monkeypatch.setattr(server, "_runtime_config", RuntimeConfig(device_address=address))
    monkeypatch.setattr(server, "_connection_control_locks", {})

    result = await server.connect_and_wait(address, 1.0)

    assert result["ok"] is True
    assert result["status"]["connection"] == ConnectionState.CONNECTED.value
    assert state.connected_address == address


async def test_audio_disconnect_reclaims_exact_lease_after_grace(monkeypatch):
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    leases = MediaLeaseManager()
    lease, _ = leases.acquire(state.call_id, "mcp_client")
    manager = AudioManager()
    manager.create_session(lease.stream_id, "AA:BB:CC:DD:EE:FF")

    class Sidecar:
        def __init__(self):
            self.attached = False

        def client_attached(self, _stream_id):
            return self.attached

        def detach_session(self, _stream_id):
            pass

    sidecar = Sidecar()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_manager", manager)
    monkeypatch.setattr(server, "_audio_stream_server", sidecar)
    monkeypatch.setattr(server, "_audio_lifecycle_locks", {})
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "_legacy_stream_aliases", {})
    monkeypatch.setattr(server, "AUDIO_CLIENT_DISCONNECT_GRACE_SECONDS", 0.0)

    server._on_audio_client_disconnected(lease.stream_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert leases.current() is None
    assert manager.get_session(lease.stream_id) is None


def test_stale_call_cleanup_generation_does_not_revoke_current_lease(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    leases = MediaLeaseManager()
    lease, _ = leases.acquire(state.call_id, "mcp_client")
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)

    server._schedule_call_end_audio_cleanup(
        state.connection_generation,
        state.call_generation - 1,
    )

    assert leases.current() == lease


async def test_audio_reconnect_cancels_disconnect_reclamation(monkeypatch):
    state = HFPState()
    state._asyncio_loop = asyncio.get_running_loop()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    leases = MediaLeaseManager()
    lease, _ = leases.acquire(state.call_id, "mcp_client")
    state.set_sco_state(
        "ready",
        owner=lease.owner,
        stream_id=lease.stream_id,
    )

    class Sidecar:
        attached = False

        def client_attached(self, _stream_id):
            return self.attached

        def detach_session(self, _stream_id):
            pass

    sidecar = Sidecar()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_audio_stream_server", sidecar)
    monkeypatch.setattr(server, "_audio_reclaim_tasks", {})
    monkeypatch.setattr(server, "AUDIO_CLIENT_DISCONNECT_GRACE_SECONDS", 0.02)

    server._on_audio_client_disconnected(lease.stream_id)
    await asyncio.sleep(0)
    assert state.audio_client_attached is False
    sidecar.attached = True
    server._on_audio_client_connected(lease.stream_id)
    await asyncio.sleep(0)
    assert state.audio_client_attached is True
    await asyncio.sleep(0.03)

    assert leases.current() == lease


async def test_active_call_without_sco_recommends_audio_acquisition(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    monkeypatch.setattr(server, "_state", state)
    context = await server.get_phone_context()
    assert context["recommended_next_action"] == "start_audio_stream"


async def test_high_level_audio_lease_rejects_second_owner(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    call_id = state.call_id
    leases = MediaLeaseManager()

    async def start(stream_id):
        return {
            "ok": True,
            "stream_url": f"ws://example.test/audio/{stream_id}?token=once",
            "audio": {"sample_rate_hz": 8000},
        }

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_request_ledger", None)
    monkeypatch.setattr(server, "start_audio_stream", start)

    first = await server.acquire_audio_stream(call_id, "gemini_live", "request-1")
    second = await server.acquire_audio_stream(call_id, "hermes_classic", "request-2")
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "audio_owner_conflict"


async def test_audio_idempotency_reissues_token_without_persisting_it(
    monkeypatch, tmp_path
):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    leases = MediaLeaseManager()
    ledger = RequestLedger(tmp_path / "calls.db")
    calls = 0

    async def start(stream_id):
        nonlocal calls
        calls += 1
        return {
            "ok": True,
            "stream_url": f"ws://example.test/audio/{stream_id}?token=secret-{calls}",
            "audio": {"sample_rate_hz": 8000},
        }

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_media_leases", leases)
    monkeypatch.setattr(server, "_request_ledger", ledger)
    monkeypatch.setattr(server, "start_audio_stream", start)

    first = await server.acquire_audio_stream(
        state.call_id, "gemini_live", "same-request"
    )
    second = await server.acquire_audio_stream(
        state.call_id, "gemini_live", "same-request"
    )
    conflict = await server.acquire_audio_stream(
        state.call_id, "hermes_classic", "same-request"
    )

    assert first["result"]["stream_id"] == second["result"]["stream_id"]
    assert first["result"]["stream_url"] != second["result"]["stream_url"]
    assert conflict["error"]["code"] == "request_id_conflict"
    assert "secret-" not in Path(ledger.path).read_bytes().decode("latin-1")
    ledger.close()


async def test_answer_rejects_outgoing_ringback_without_sending_at(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.RINGING)
    monkeypatch.setattr(server, "_state", state)
    result = await server.answer_call()
    assert result["ok"] is False
    assert "No incoming call" in result["error"]


async def test_gemini_stream_wrapper_acquires_explicit_owner(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    seen = []

    async def acquire(call_id, owner, request_id):
        seen.append((call_id, owner, request_id))
        return {
            "ok": True,
            "result": {
                "stream_id": "audio-1",
                "stream_url": "ws://example.test/audio-1?token=once",
                "audio": {"sample_rate_hz": 8000},
            },
        }

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "acquire_audio_stream", acquire)

    result = await server._acquire_gemini_stream("ignored-alias")

    assert result["ok"] is True
    assert result["stream_id"] == "audio-1"
    assert result["client_stream_url"].endswith("token=once")
    assert seen[0][0:2] == (state.call_id, "gemini_live")
    assert seen[0][2].startswith(f"gemini-audio-{state.call_id}-")


async def test_unknown_live_caller_gets_no_declared_bridge_tools(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    seen = []

    class FakeManager:
        async def start(self, session_id, initial_context):
            seen.append((session_id, initial_context))
            return {"ok": True}

    def manager(allowed_tools=None):
        seen.append(allowed_tools)
        return FakeManager()

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_gemini_live_manager", None)
    monkeypatch.setattr(server, "_gemini_allowed_tools", None)
    monkeypatch.setattr(server, "_prepare_live_ai_manager", manager)
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)

    result = await server.start_live_ai_call(
        "active-call", "restricted conversation", "unknown"
    )

    assert result["ok"] is True
    assert seen[0] == frozenset()
    assert seen[1][0] == state.call_id


async def test_live_tools_use_authoritative_state_role_not_client_hint(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE, direction=CallDirection.INCOMING)
    state.set_remote_identity(
        "+14155552671",
        source="clip",
        caller_role="admin",
    )
    seen = []

    class FakeManager:
        async def start(self, session_id, initial_context):
            seen.append((session_id, initial_context))
            return {"ok": True}

    def manager(allowed_tools=None):
        seen.append(allowed_tools)
        return FakeManager()

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_gemini_live_manager", None)
    monkeypatch.setattr(server, "_gemini_allowed_tools", None)
    monkeypatch.setattr(server, "_prepare_live_ai_manager", manager)
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)

    result = await server.start_live_ai_call(
        "active-call", "admin conversation", "unknown"
    )

    assert result["ok"] is True
    assert seen[0] == frozenset(server.HERMES_TOOL_NAMES)


async def test_running_live_session_rejects_authoritative_policy_mismatch(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE, direction=CallDirection.INCOMING)
    state.set_remote_identity(
        "+14155552671",
        source="clip",
        caller_role="admin",
    )

    class RunningRestrictedManager:
        running = True

        async def start(self, *_args):
            raise AssertionError("mismatched running manager must not be reused")

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_gemini_live_manager", RunningRestrictedManager())
    monkeypatch.setattr(server, "_gemini_allowed_tools", frozenset())

    result = await server.start_live_ai_call(caller_role="admin")

    assert result["ok"] is False
    assert result["error"]["code"] == "live_ai_policy_conflict"


async def test_live_manager_reads_preserve_failed_restricted_manager(monkeypatch):
    class FailedManager:
        running = False

        def status(self, _call_context=None):
            return {
                "ok": True,
                "state": "failed",
                "last_error": "provider setup rejected",
            }

        async def poll_requests(self, _timeout_seconds):
            return {"ok": True, "requests": [{"request_id": "failed-request"}]}

        def pending_requests(self):
            return {"ok": True, "requests": [{"request_id": "pending-request"}]}

    failed = FailedManager()
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE, direction=CallDirection.INCOMING)
    state.set_remote_identity(
        "+14155552671",
        source="clip",
        caller_role="trusted",
    )
    created = []
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_gemini_live_manager", failed)
    monkeypatch.setattr(server, "_gemini_allowed_tools", frozenset())
    monkeypatch.setattr(
        server,
        "_new_live_ai_manager",
        lambda allowed_tools: created.append(allowed_tools),
    )
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)

    status = await server.get_live_ai_status()
    polled = await server.poll_live_ai_requests(0.0)
    pending = server.get_live_ai_pending_requests()

    assert status["state"] == "failed"
    assert status["last_error"] == "provider setup rejected"
    assert status["caller_role"] == "trusted"
    assert status["remote_number_verified"] is True
    assert polled["requests"][0]["request_id"] == "failed-request"
    assert pending["requests"][0]["request_id"] == "pending-request"
    assert server._gemini_live_manager is failed
    assert created == []


async def test_explicit_live_call_setup_changes_stopped_manager_policy(monkeypatch):
    state = HFPState()
    state.connection_state = ConnectionState.CONNECTED
    state.set_call_state(CallState.ACTIVE)
    starts = []
    created = []

    class StoppedManager:
        running = False

    class ReplacementManager:
        running = False

        async def start(self, session_id, initial_context):
            starts.append((session_id, initial_context))
            return {"ok": True}

    stopped = StoppedManager()
    replacement = ReplacementManager()

    def new_manager(allowed_tools):
        created.append(allowed_tools)
        return replacement

    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_gemini_live_manager", stopped)
    monkeypatch.setattr(
        server, "_gemini_allowed_tools", frozenset(server.HERMES_TOOL_NAMES)
    )
    monkeypatch.setattr(server, "_new_live_ai_manager", new_manager)
    monkeypatch.setattr(server, "_sync_live_ai_state", lambda: None)

    result = await server.start_live_ai_call(
        "active-call", "restricted conversation", "unknown"
    )

    assert result["ok"] is True
    assert created == [frozenset()]
    assert starts == [(state.call_id, "restricted conversation")]
    assert server._gemini_live_manager is replacement
    assert server._gemini_allowed_tools == frozenset()


def test_server_transcript_tool_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(server, "_runtime_config", RuntimeConfig())
    monkeypatch.setattr(
        server,
        "_get_live_ai_manager",
        lambda: (_ for _ in ()).throw(AssertionError("manager should not be read")),
    )

    result = server.get_call_transcript("call-1")

    assert result["ok"] is False
    assert result["error"]["code"] == "privacy_disabled"


def test_capabilities_report_transcript_privacy_and_redacted_summaries(monkeypatch):
    monkeypatch.setattr(server, "_runtime_config", RuntimeConfig(full_transcripts=False))

    result = server.get_capabilities()

    assert result["features"]["call_transcript_history"] is False
    assert result["features"]["redacted_call_summaries"] is True
    assert "record_call_summary" in result["preferred_workflows"]["post_call"]


async def test_record_call_summary_redacts_pii_and_is_idempotent(
    monkeypatch, tmp_path
):
    ledger = RequestLedger(tmp_path / "summary.db")
    state = HFPState()
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_request_ledger", ledger)
    monkeypatch.setattr(server, "_request_locks", {})
    monkeypatch.setattr(server, "_last_ended_call_id", "call-summary-1")
    monkeypatch.setattr(server, "_last_ended_call_number", "+14155550100")
    monkeypatch.setattr(server, "_last_ended_call_started_at", 123.0)

    try:
        first = await server.record_call_summary(
            "call-summary-1",
            "Call +1 415 555 0100 or me@example.com; details at https://example.com/a",
            "unknown",
            "summary-request-1",
        )
        replay = await server.record_call_summary(
            "call-summary-1",
            "Call +1 415 555 0100 or me@example.com; details at https://example.com/a",
            "unknown",
            "summary-request-1",
        )
        conflict = await server.record_call_summary(
            "call-summary-1",
            "different role",
            "trusted",
            "summary-request-1",
        )
        stored = ledger.get_call_summary("call-summary-1")
    finally:
        ledger.close()

    assert first == replay
    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "request_id_conflict"
    assert stored is not None
    assert stored["summary"] == "Call [number] or [email]; details at [url]"
