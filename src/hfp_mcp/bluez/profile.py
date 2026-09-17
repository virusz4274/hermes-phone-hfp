"""BlueZ ``org.bluez.Profile1`` implementation for the HFP HF role."""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Callable

import dbus
import dbus.service

from ..config import (
    BLUEZ_PROFILE_MANAGER_IFACE,
    BLUEZ_PROFILE_MANAGER_PATH,
    BLUEZ_SERVICE,
    HFP_HF_UUID,
    HFP_PROFILE_PATH,
    HFP_SDP_FEATURES as CONFIG_HFP_SDP_FEATURES,
)

log = logging.getLogger(__name__)

PROFILE1_IFACE = "org.bluez.Profile1"

# SDP Hands-Free SupportedFeatures bits are *not* AT+BRSF bits.  In
# particular, SDP bit 5 means wide-band speech, which this CVSD-only bridge
# must not advertise.  Caller-ID presentation is the only SDP feature backed
# end-to-end by the core session implementation today.
SDP_FEATURE_EC_NR = 1 << 0
SDP_FEATURE_THREE_WAY = 1 << 1
SDP_FEATURE_CLI_PRESENTATION = 1 << 2
SDP_FEATURE_VOICE_RECOGNITION = 1 << 3
SDP_FEATURE_REMOTE_VOLUME = 1 << 4
SDP_FEATURE_WIDEBAND_SPEECH = 1 << 5
HFP_SDP_FEATURES = CONFIG_HFP_SDP_FEATURES


class BlueZProfileRejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class RFCOMMConnectionRegistry:
    """Own at most one transferred RFCOMM descriptor per phone address."""

    def __init__(self, *, max_connections: int = 1) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self._lock = threading.Lock()
        self._sockets: dict[str, socket.socket] = {}
        self._max_connections = max_connections

    def claim(self, address: str, sock: socket.socket) -> bool:
        stale: list[socket.socket] = []
        accepted = False
        with self._lock:
            for stale_address, existing_socket in tuple(self._sockets.items()):
                if existing_socket.fileno() < 0:
                    stale.append(self._sockets.pop(stale_address))
            existing = self._sockets.get(address)
            if existing is None and len(self._sockets) < self._max_connections:
                self._sockets[address] = sock
                accepted = True
        for stale_socket in stale:
            _close_socket(stale_socket)
        return accepted

    def close(
        self,
        address: str,
        *,
        expected_socket: socket.socket | None = None,
    ) -> bool:
        with self._lock:
            sock = self._sockets.get(address)
            if sock is None or (
                expected_socket is not None and sock is not expected_socket
            ):
                return False
            self._sockets.pop(address, None)
        _close_socket(sock)
        return True

    def close_all(self) -> tuple[str, ...]:
        with self._lock:
            items = tuple(self._sockets.items())
            self._sockets.clear()
        for _, sock in items:
            _close_socket(sock)
        return tuple(address for address, _ in items)

    def active_addresses(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                address
                for address, sock in self._sockets.items()
                if sock.fileno() >= 0
            )


class HFPProfile(dbus.service.Object):
    """Profile1 object with deterministic descriptor ownership."""

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
        self._connections = RFCOMMConnectionRegistry()

    @dbus.service.method(PROFILE1_IFACE, in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, fd_properties):  # noqa: N802
        address = _path_to_address(str(device))
        raw_fd = _take_fd(fd)
        sock: socket.socket | None = None
        claimed = False
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM,
                fileno=raw_fd,
            )
            sock.setblocking(True)
            claimed = self._connections.claim(address, sock)
            if not claimed:
                _close_socket(sock)
                raise BlueZProfileRejected(
                    f"An RFCOMM connection for {address} is already active"
                )
            props = {str(key): value for key, value in fd_properties.items()}
            log.info("NewConnection from %s (fd=%d, props=%s)", address, raw_fd, props)
            try:
                self._on_new_connection(address, sock, props)
            except Exception as exc:
                self._connections.close(address, expected_socket=sock)
                if isinstance(exc, dbus.exceptions.DBusException):
                    raise
                raise BlueZProfileRejected(str(exc)) from exc
        except Exception:
            if sock is None:
                try:
                    os.close(raw_fd)
                except OSError:
                    pass
            elif not claimed or address not in self._connections.active_addresses():
                _close_socket(sock)
            raise

    @dbus.service.method(PROFILE1_IFACE, in_signature="o", out_signature="")
    def RequestDisconnection(self, device):  # noqa: N802
        address = _path_to_address(str(device))
        # BlueZ requires every descriptor for the device to be released before
        # this method returns.  Closing first also wakes the RFCOMM owner.
        self._connections.close(address)
        log.info("RequestDisconnection from %s", address)
        self._on_request_disconnection(address)

    @dbus.service.method(PROFILE1_IFACE, in_signature="", out_signature="")
    def Release(self):  # noqa: N802
        addresses = self._connections.close_all()
        log.warning("HFP profile released by BlueZ (connections=%s)", addresses)
        self._on_release()

    def close_connection(
        self,
        address: str,
        expected_socket: socket.socket | None = None,
    ) -> bool:
        """Close one connection during local shutdown or handshake failure."""

        return self._connections.close(
            address.upper(),
            expected_socket=expected_socket,
        )

    def close_all_connections(self) -> tuple[str, ...]:
        return self._connections.close_all()

    @property
    def active_addresses(self) -> tuple[str, ...]:
        return self._connections.active_addresses()


def register_hfp_profile(
    bus: dbus.SystemBus,
    *,
    sdp_features: int = HFP_SDP_FEATURES,
    channel: int | None = None,
) -> None:
    """Register the process as BlueZ's single HFP Hands-Free owner."""

    pm = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, BLUEZ_PROFILE_MANAGER_PATH),
        BLUEZ_PROFILE_MANAGER_IFACE,
    )
    options = {
        "Name": dbus.String("HFP Hands-Free MCP"),
        "Version": dbus.UInt16(0x0108),
        "Features": dbus.UInt16(sdp_features),
        "RequireAuthentication": dbus.Boolean(True),
        "RequireAuthorization": dbus.Boolean(False),
        "AutoConnect": dbus.Boolean(True),
    }
    # Omitting Channel selects BlueZ's stable HFP HF default (RFCOMM 7).
    if channel is not None:
        options["Channel"] = dbus.UInt16(channel)
    pm.RegisterProfile(HFP_PROFILE_PATH, HFP_HF_UUID, options)
    log.info(
        "HFP HF profile registered (UUID=%s, SDP features=0x%x)",
        HFP_HF_UUID,
        sdp_features,
    )


def unregister_hfp_profile(bus: dbus.SystemBus) -> None:
    pm = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, BLUEZ_PROFILE_MANAGER_PATH),
        BLUEZ_PROFILE_MANAGER_IFACE,
    )
    pm.UnregisterProfile(HFP_PROFILE_PATH)


def _take_fd(fd) -> int:
    """Transfer a D-Bus UnixFd into this process without duplicating it."""

    take = getattr(fd, "take", None)
    if callable(take):
        return int(take())
    # Useful for direct unit tests and bindings that already unwrap UnixFd.
    if type(fd) is int:
        return fd
    raise TypeError("D-Bus UnixFd does not support ownership transfer via take()")


def _path_to_address(path: str) -> str:
    marker = "/dev_"
    if marker not in path:
        raise ValueError(f"Invalid BlueZ device path: {path!r}")
    return path.rsplit(marker, 1)[1].replace("_", ":").upper()


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
