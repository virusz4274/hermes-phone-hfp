import asyncio
import json
import time
from types import SimpleNamespace
import pytest
from hfp_mcp.contracts import RequestLedger
from hfp_mcp.state import HFPState, CallState
from hfp_mcp.hfp.protocol import CallDirection
from hfp_mcp.workflow_execution import WorkflowExecution
from hfp_mcp.routing import RoutingConfig
from tests.test_phone_routing import routing_data


@pytest.fixture
def execution(tmp_path, monkeypatch):
    ledger = RequestLedger(tmp_path / 'calls.db')
    state = HFPState()
    calls = []
    async def start(number, request_id, purpose, **kwargs):
        assert kwargs == {'workflow_brief': True}
        calls.append((number, request_id, purpose))
        return {'ok': False, 'error': {'code': 'timeout_cancelled'}}
    cfg = RoutingConfig.parse(routing_data())
    monkeypatch.setattr(RoutingConfig, 'load', classmethod(lambda cls: cfg))
    from hfp_mcp import hermes_api
    class API:
        def __init__(self, *a): pass
        async def request(self, *a, **kw):
            return {'authorized': True, 'profile': 'default', 'number': '+919876543210', 'purpose': 'Call purpose.',
                    'attempt_id': kw['json']['attempt_id'], 'deadline': time.time()+300}
        async def close(self): pass
    monkeypatch.setattr(hermes_api, 'HermesAPI', API)
    runtime = SimpleNamespace(ledger=lambda: ledger, state=state, start_call=start)
    manager = WorkflowExecution(runtime, SimpleNamespace())
    yield manager, calls, state, ledger
    ledger.close()


async def test_daemon_duplicate_attempt_calls_once(execution):
    manager, calls, _, _ = execution
    body = {'endpoint': 'owner', 'callback_id': 'cb-request', 'attempt_id': 'attempt', 'execution_id': 'native'}
    await asyncio.gather(manager.start(body), manager.start(body))
    await asyncio.gather(*manager.tasks)
    assert len(calls) == 1
    outcome = manager.outcome('attempt')
    assert outcome['finished'] and outcome['outcome']['connection_state'] == 'not_connected'
    assert outcome['outcome']['termination_reason'] == 'answer_timeout_cancelled'


async def test_restart_starting_attempt_is_uncertain_without_redial(execution):
    manager, calls, _, _ = execution
    row = {'id': 'attempt', 'number': '+919876543210', 'profile': 'default', 'started_at': time.time(),
           'call_generation': 1, 'call_id': None, 'status': 'starting', 'result': None, 'envelope': {}}
    manager.save(row)
    restarted = WorkflowExecution(manager.runtime, manager.config)
    assert restarted.get('attempt')['status'] == 'uncertain'
    assert restarted.outcome('attempt')['outcome']['uncertain']
    assert not calls


def test_exact_call_outcome_never_reads_subsequent_call(execution):
    manager, _, state, ledger = execution
    now = time.time()
    row = {'id': 'attempt', 'number': '+919876543210', 'profile': 'default', 'started_at': now,
           'call_generation': 1, 'call_id': 'original', 'status': 'observing', 'result': {'ok': True}, 'envelope': {}}
    manager.save(row)
    ledger.audit('call_ended', 'ok', call_id='original', detail={'was_active': True, 'termination_reason': 'no_carrier'})
    ledger.append_transcript({'session_id': 'original', 'provider': 'gemini', 'direction': 'input', 'text': 'Call tomorrow at ten.', 'timestamp': now})
    ledger.append_transcript({'session_id': 'another', 'provider': 'gemini', 'direction': 'input', 'text': 'Private other call.', 'timestamp': now})
    state.set_call_state(CallState.ACTIVE, direction='outgoing')
    result = manager.outcome('attempt')['outcome']
    assert result['connection_state'] == 'connected' and not result['still_active']
    assert result['evidence'][0]['text'] == 'Call tomorrow at ten.'
    assert 'Private' not in json.dumps(result)


@pytest.mark.parametrize('reason', ['busy', 'no_answer', 'transport_disconnected'])
def test_preserved_transport_termination_reasons(execution, reason):
    manager, _, _, ledger = execution
    row = {'id': 'attempt', 'number': '+919876543210', 'profile': 'default', 'started_at': time.time(),
           'call_generation': 1, 'call_id': 'call', 'status': 'observing', 'result': {'ok': False}, 'envelope': {}}
    manager.save(row)
    ledger.audit('call_ended', 'ok', call_id='call', detail={'was_active': False, 'termination_reason': reason})
    result = manager.outcome('attempt')['outcome']
    assert result['termination_reason'] == reason
    assert result['uncertain'] == (reason == 'transport_disconnected')
