"""BlueZ profile ownership and exact device-selection tests."""

from __future__ import annotations

import socket

import pytest

from hfp_mcp.bluez import manager as manager_module
from hfp_mcp.bluez import profile as profile_module
from hfp_mcp.bluez.manager import BlueZManager
from hfp_mcp.bluez.profile import (
    HFP_SDP_FEATURES,
    SDP_FEATURE_WIDEBAND_SPEECH,
    BlueZProfileRejected,
    HFPProfile,
    RFCOMMConnectionRegistry,
    _take_fd,
    register_hfp_profile,
)
from hfp_mcp.config import (
    BLUEZ_ADAPTER_IFACE,
    BLUEZ_DEVICE_IFACE,
    HFP_AG_UUID,
)
from hfp_mcp.hfp.handshake import HF_BRSF_FEATURES


class _ManagedObjects:
    def __init__(self, objects):
        self.objects = objects

    def GetManagedObjects(self, **kwargs):  # noqa: N802
        self.timeout = kwargs.get("timeout")
        return self.objects


class _Device:
    def __init__(self):
        self.calls = []

    def ConnectProfile(self, uuid, **kwargs):  # noqa: N802
        self.calls.append(("connect_profile", str(uuid), kwargs))

    def Connect(self, **kwargs):  # noqa: N802
        self.calls.append(("connect", kwargs))

    def DisconnectProfile(self, uuid, **kwargs):  # noqa: N802
        self.calls.append(("disconnect_profile", str(uuid), kwargs))


class _Bus:
    def __init__(self, root, devices=None):
        self.root = root
        self.devices = devices or {}

    def get_object(self, _service, path):
        if path == "/":
            return self.root
        return self.devices[path]


def _objects():
    return {
        "/org/bluez/hci0": {BLUEZ_ADAPTER_IFACE: {"Address": "00:00:00:00:00:01"}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            BLUEZ_DEVICE_IFACE: {
                "Address": "AA:BB:CC:DD:EE:FF",
                "Alias": "Phone alias",
                "Name": "Phone name",
                "Paired": True,
                "Trusted": True,
                "Connected": False,
                "ServicesResolved": True,
                "UUIDs": [HFP_AG_UUID],
            }
        },
        "/org/bluez/hci0/dev_11_22_33_44_55_66": {
            BLUEZ_DEVICE_IFACE: {
                "Address": "11:22:33:44:55:66",
                "Paired": False,
                "UUIDs": [HFP_AG_UUID],
            }
        },
    }


def _manager(monkeypatch, objects=None):
    objects = _objects() if objects is None else objects
    path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    device = _Device()
    bus = _Bus(_ManagedObjects(objects), {path: device})
    monkeypatch.setattr(manager_module.dbus, "Interface", lambda obj, _iface: obj)
    return BlueZManager(bus), device


def test_manager_filters_unpaired_devices_and_uses_alias(monkeypatch):
    manager, _ = _manager(monkeypatch)
    assert manager.find_adapter() == "/org/bluez/hci0"
    assert manager.get_paired_hfp_devices() == [
        {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Phone alias",
            "connected": False,
            "paired": True,
            "trusted": True,
            "services_resolved": True,
            "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        }
    ]


def test_manager_refuses_ambiguous_adapter_selection(monkeypatch):
    objects = _objects()
    objects["/org/bluez/hci1"] = {BLUEZ_ADAPTER_IFACE: {}}
    manager, _ = _manager(monkeypatch, objects)
    with pytest.raises(RuntimeError, match="Multiple Bluetooth adapters"):
        manager.find_adapter()


def test_manager_selects_configured_adapter(monkeypatch):
    objects = _objects()
    objects["/org/bluez/hci1"] = {
        BLUEZ_ADAPTER_IFACE: {"Address": "00:00:00:00:00:02"}
    }
    root = _ManagedObjects(objects)
    bus = _Bus(root)
    monkeypatch.setattr(manager_module.dbus, "Interface", lambda obj, _iface: obj)
    manager = BlueZManager(bus, adapter="hci1")
    assert manager.find_adapter() == "/org/bluez/hci1"


def test_manager_selects_adapter_by_exact_mac_address(monkeypatch):
    objects = _objects()
    objects["/org/bluez/hci1"] = {
        BLUEZ_ADAPTER_IFACE: {"Address": "12:34:56:78:9A:BC"}
    }
    root = _ManagedObjects(objects)
    bus = _Bus(root)
    monkeypatch.setattr(manager_module.dbus, "Interface", lambda obj, _iface: obj)
    manager = BlueZManager(bus, adapter="12-34-56-78-9a-bc")
    assert manager.find_adapter() == "/org/bluez/hci1"


def test_connectable_does_not_enable_pairing_or_discovery(monkeypatch):
    properties = {"Connectable": False, "Pairable": False, "Discoverable": False}

    class Adapter:
        def Set(self, interface, name, value):  # noqa: N802
            assert interface == BLUEZ_ADAPTER_IFACE
            properties[name] = bool(value)

    bus = _Bus(_ManagedObjects(_objects()), {"/org/bluez/hci0": Adapter()})
    monkeypatch.setattr(manager_module.dbus, "Interface", lambda obj, _iface: obj)
    manager = BlueZManager(bus)
    manager.find_adapter()
    manager.set_connectable(True)

    assert properties == {
        "Connectable": True, "Pairable": False, "Discoverable": False,
    }


@pytest.mark.parametrize("error_name", [
    "org.freedesktop.DBus.Error.UnknownProperty",
    "org.freedesktop.DBus.Error.AccessDenied",
])
def test_connectable_supports_old_bluez_but_does_not_hide_denials(monkeypatch, error_name):
    manager, _ = _manager(monkeypatch)

    def fail_set(*args):
        raise manager_module.dbus.exceptions.DBusException("property error", name=error_name)

    monkeypatch.setattr(manager, "_set_adapter_property", fail_set)
    if error_name.endswith("UnknownProperty"):
        manager.set_connectable(True)
    else:
        with pytest.raises(manager_module.dbus.exceptions.DBusException):
            manager.set_connectable(True)


def test_disconnected_phone_connects_hfp_without_generic_device_connect(monkeypatch):
    manager, device = _manager(monkeypatch)
    manager.find_adapter()
    manager.connect_device("aa-bb-cc-dd-ee-ff")
    manager.disconnect_device("AA:BB:CC:DD:EE:FF")

    assert device.calls == [
        ("connect_profile", HFP_AG_UUID, {"timeout": 15.0}),
        ("disconnect_profile", HFP_AG_UUID, {"timeout": 15.0}),
    ]


def test_connected_phone_reconnects_only_hfp_profile(monkeypatch):
    objects = _objects()
    device_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    objects[device_path][BLUEZ_DEVICE_IFACE]["Connected"] = True
    manager, device = _manager(monkeypatch, objects)
    manager.find_adapter()

    manager.connect_device("AA:BB:CC:DD:EE:FF")

    assert device.calls == [
        ("connect_profile", HFP_AG_UUID, {"timeout": 15.0}),
    ]


def test_manager_does_not_fabricate_unknown_device_path(monkeypatch):
    manager, _ = _manager(monkeypatch)
    manager.find_adapter()
    with pytest.raises(RuntimeError, match="was not found"):
        manager.connect_device("00:11:22:33:44:55")


def test_sdp_and_brsf_masks_are_separate_and_wbs_is_not_advertised():
    assert HFP_SDP_FEATURES != HF_BRSF_FEATURES
    assert not HFP_SDP_FEATURES & SDP_FEATURE_WIDEBAND_SPEECH


def test_profile_registration_omits_dynamic_channel_and_wbs(monkeypatch):
    class _ProfileManager:
        def RegisterProfile(self, path, uuid, options):  # noqa: N802
            self.args = (str(path), str(uuid), options)

    profile_manager = _ProfileManager()
    bus = _Bus(profile_manager, {"/org/bluez": profile_manager})
    monkeypatch.setattr(profile_module.dbus, "Interface", lambda obj, _iface: obj)
    register_hfp_profile(bus)
    _, _, options = profile_manager.args

    assert int(options["Features"]) == HFP_SDP_FEATURES
    assert not int(options["Features"]) & SDP_FEATURE_WIDEBAND_SPEECH
    assert "Channel" not in options


def test_connection_registry_rejects_duplicate_and_closes_deterministically():
    registry = RFCOMMConnectionRegistry()
    first, first_peer = socket.socketpair()
    second, second_peer = socket.socketpair()
    try:
        assert registry.claim("AA:BB:CC:DD:EE:FF", first) is True
        assert registry.claim("AA:BB:CC:DD:EE:FF", second) is False
        assert registry.active_addresses() == ("AA:BB:CC:DD:EE:FF",)
        assert registry.close("AA:BB:CC:DD:EE:FF") is True
        assert first.fileno() == -1
        assert registry.active_addresses() == ()
    finally:
        second.close()
        first_peer.close()
        second_peer.close()


def test_connection_registry_rejects_a_second_phone():
    registry = RFCOMMConnectionRegistry()
    first, first_peer = socket.socketpair()
    second, second_peer = socket.socketpair()
    try:
        assert registry.claim("AA:BB:CC:DD:EE:FF", first) is True
        assert registry.claim("11:22:33:44:55:66", second) is False
        assert registry.active_addresses() == ("AA:BB:CC:DD:EE:FF",)
    finally:
        registry.close_all()
        second.close()
        first_peer.close()
        second_peer.close()


def test_connection_registry_stale_close_cannot_close_replacement():
    registry = RFCOMMConnectionRegistry()
    first, first_peer = socket.socketpair()
    replacement, replacement_peer = socket.socketpair()
    address = "AA:BB:CC:DD:EE:FF"
    try:
        assert registry.claim(address, first) is True
        first.close()
        assert registry.claim(address, replacement) is True

        assert registry.close(address, expected_socket=first) is False
        assert replacement.fileno() >= 0
        assert registry.active_addresses() == (address,)
        assert registry.close(address, expected_socket=replacement) is True
    finally:
        first_peer.close()
        replacement_peer.close()


def test_take_fd_requires_explicit_ownership_transfer():
    class _FD:
        def take(self):
            return 42

    assert _take_fd(_FD()) == 42
    with pytest.raises(TypeError, match="ownership transfer"):
        _take_fd(object())


class _FakeSocket:
    _next_fd = 100

    def __init__(self):
        self.fd = self._next_fd
        type(self)._next_fd += 1
        self.closed = False

    def setblocking(self, _blocking):
        pass

    def fileno(self):
        return -1 if self.closed else self.fd

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


def _bare_profile(on_new, on_disconnect=lambda _address: None):
    profile = object.__new__(HFPProfile)
    profile._on_new_connection = on_new
    profile._on_request_disconnection = on_disconnect
    profile._on_release = lambda: None
    profile._connections = RFCOMMConnectionRegistry()
    return profile


def test_profile_closes_fd_before_disconnection_callback(monkeypatch):
    sockets = []

    def socket_factory(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    callback_closed = []
    profile = _bare_profile(
        lambda *_args: None,
        lambda _address: callback_closed.append(sockets[0].closed),
    )
    monkeypatch.setattr(profile_module.socket, "socket", socket_factory)
    profile.NewConnection(
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        77,
        {},
    )
    profile.RequestDisconnection(
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    )
    assert callback_closed == [True]


def test_profile_rejects_duplicate_connection_and_closes_new_fd(monkeypatch):
    sockets = []

    def socket_factory(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    profile = _bare_profile(lambda *_args: None)
    monkeypatch.setattr(profile_module.socket, "socket", socket_factory)
    path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    profile.NewConnection(path, 77, {})
    with pytest.raises(BlueZProfileRejected):
        profile.NewConnection(path, 78, {})
    assert sockets[0].closed is False
    assert sockets[1].closed is True
