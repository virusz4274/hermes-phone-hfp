"""Execution identity compatibility must never substitute conversation authority."""
import sys
from contextvars import ContextVar
from types import ModuleType

import pytest

from hfp_mcp.hermes_compat import current_run_id


@pytest.fixture
def execution(monkeypatch):
    module = ModuleType('gateway.session_api_turn')
    module.api_execution = ContextVar('api_execution', default=None)
    module.current_api_run_id = lambda: (module.api_execution.get() or {}).get('run_id')
    approval = ModuleType('tools.approval_context')
    approval.get_current_session_key = lambda: 'run_legacy'
    monkeypatch.setitem(sys.modules, 'gateway.session_api_turn', module)
    monkeypatch.setitem(sys.modules, 'tools.approval_context', approval)
    return module


def test_canonical_identity_precedes_approval_route(execution):
    execution.api_execution.set({'run_id': 'run_canonical'})
    assert current_run_id() == 'run_canonical'


def test_canonical_execution_without_run_fails_closed(execution):
    execution.api_execution.set({})
    with pytest.raises(PermissionError, match='no admitted run'):
        current_run_id()


def test_legacy_direct_run_remains_supported(execution):
    assert current_run_id() == 'run_legacy'
    del execution.current_api_run_id
    assert current_run_id() == 'run_legacy'
