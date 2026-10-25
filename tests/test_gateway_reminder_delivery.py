"""Gateway selection delegates to Hermes rather than duplicating platform policy."""
import sys
from types import ModuleType

import pytest

from hfp_mcp.hermes_compat import gateway_destination, validate_gateway_destination


@pytest.fixture
def native_delivery(monkeypatch):
    module = ModuleType('cron.scheduler_delivery')
    module.target = {'platform': 'slack', 'chat_id': 'owner', 'thread_id': '42'}
    module.targets = [{'id': 'telegram', 'home_target_set': True},
                      {'id': 'slack', 'home_target_set': True}]
    module.calls = []
    def resolve(job):
        module.calls.append(job)
        return module.target
    module._resolve_delivery_target = resolve
    module.cron_delivery_targets = lambda: module.targets
    monkeypatch.setitem(sys.modules, 'cron.scheduler_delivery', module)
    return module


def test_uses_native_default_without_api_origin_or_broadcast(native_delivery):
    selected = gateway_destination()
    assert selected == {'platform': 'slack', 'chat_id': 'owner', 'thread_id': '42',
                        'deliver': 'slack:owner:42'}
    assert native_delivery.calls == [{'deliver': 'origin', 'origin': None}]


def test_retained_destination_survives_changed_home_but_requires_enabled_transport(native_delivery):
    selected = gateway_destination()
    native_delivery.target = {'platform': 'telegram', 'chat_id': 'new-home'}
    validate_gateway_destination(selected)
    assert selected['deliver'] == 'slack:owner:42'
    native_delivery.targets = [{'id': 'telegram', 'home_target_set': True}]
    with pytest.raises(ValueError, match='no longer configured'):
        validate_gateway_destination(selected)


@pytest.mark.parametrize('target', [None, {'platform': 'api_server', 'chat_id': 'phone'},
                                  {'platform': 'unconfigured', 'chat_id': 'home'}])
def test_missing_or_nonpush_default_is_not_silently_accepted(native_delivery, target):
    native_delivery.target = target
    with pytest.raises(ValueError, match='no available default'):
        gateway_destination()
