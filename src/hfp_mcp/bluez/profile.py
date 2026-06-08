"""
HFPProfile — D-Bus object implementing org.bluez.Profile1.

BlueZ calls NewConnection(device_path, fd, fd_properties) when the phone's
HFP Audio Gateway connects to our registered Hands-Free profile.

Critical fd handling:
  - fd arrives as dbus.types.UnixFd
  - dbus-python >= 1.2.16 exposes .take() which transfers ownership
  - Older versions: use int(fd) which also transfers ownership
  - Wrap with socket.socket(fileno=raw_fd) — NOT socket.fromfd() which duplicates
"""

from __future__ import annotations

import logging
import socket
from typing import Callable

import dbus
import dbus.service

from ..config import (
    BLUEZ_PROFILE_MANAGER_IFACE,
    BLUEZ_PROFILE_MANAGER_PATH,
    BLUEZ_SERVICE,
    HFP_HF_FEATURES,
    HFP_HF_UUID,
    HFP_PROFILE_PATH,
)

log = logging.getLogger(__name__)

PROFILE1_IFACE = "org.bluez.Profile1"


class HFPProfile(dbus.service.Object):
    """Implements org.bluez.Profile1 at HFP_PROFILE_PATH."""

    def __init__(
        self,
        bus: dbus.SystemBus,
        on_new_connection: Callable[[str, socket.socket, dict], None],
        on_request_disconnection: Callable[[str], None],
        on_release: Callable[[], None],
    ) -> None:
        super().__init__(bus, HFP_PROFILE_PATH)
        self._on_new_connection = on_new_connection
        self._on_request_disconnection = on_request_disconnection
        self._on_release = on_release

    @dbus.service.method(PROFILE1_IFACE, in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, fd_properties):  # noqa: N802
        raw_fd = _take_fd(fd)
        sock = socket.socket(
            socket.AF_BLUETOOTH,
            socket.SOCK_STREAM,
            socket.BTPROTO_RFCOMM,
            fileno=raw_fd,
        )
        sock.setblocking(True)
        address = _path_to_address(str(device))
        props = {str(k): v for k, v in fd_properties.items()}
        log.info("NewConnection from %s (fd=%d, props=%s)", address, raw_fd, props)
        self._on_new_connection(address, sock, props)

    @dbus.service.method(PROFILE1_IFACE, in_signature="o", out_signature="")
    def RequestDisconnection(self, device):  # noqa: N802
        address = _path_to_address(str(device))
        log.info("RequestDisconnection from %s", address)
        self._on_request_disconnection(address)

    @dbus.service.method(PROFILE1_IFACE, in_signature="", out_signature="")
    def Release(self):  # noqa: N802
        log.warning("HFP profile released by BlueZ")
        self._on_release()


def register_hfp_profile(bus: dbus.SystemBus) -> None:
    """Register our HFP Hands-Free profile with BlueZ ProfileManager1."""
    pm = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, BLUEZ_PROFILE_MANAGER_PATH),
        BLUEZ_PROFILE_MANAGER_IFACE,
    )
    options = {
        "Name": dbus.String("HFP Hands-Free MCP"),
        "Version": dbus.UInt16(0x0108),           # HFP 1.8
        "Features": dbus.UInt16(HFP_HF_FEATURES),
        "RequireAuthentication": dbus.Boolean(True),
        "RequireAuthorization": dbus.Boolean(False),
        "AutoConnect": dbus.Boolean(True),
        "Channel": dbus.UInt16(0),                # 0 = SDP-assigned
    }
    pm.RegisterProfile(HFP_PROFILE_PATH, HFP_HF_UUID, options)
    log.info("HFP HF profile registered (UUID=%s)", HFP_HF_UUID)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _take_fd(fd) -> int:
    """Transfer ownership of a D-Bus UnixFd to a plain Python int."""
    if hasattr(fd, "take"):
        return fd.take()
    return int(fd)


def _path_to_address(path: str) -> str:
    """Convert /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF → AA:BB:CC:DD:EE:FF."""
    return path.split("/dev_")[-1].replace("_", ":")
