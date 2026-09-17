"""Deterministic BlueZ adapter and HFP device operations."""

from __future__ import annotations

import logging
import re
from typing import Optional

import dbus

from ..config import (
    BLUEZ_ADAPTER_IFACE,
    BLUEZ_DEVICE_IFACE,
    BLUEZ_SERVICE,
    DBUS_OM_IFACE,
    DBUS_PROPS_IFACE,
    HFP_AG_UUID,
)

log = logging.getLogger(__name__)

DEFAULT_DBUS_TIMEOUT_SECONDS = 15.0
_ADDRESS_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")


class BlueZManager:
    def __init__(
        self,
        bus: dbus.SystemBus,
        adapter: str | None = None,
        *,
        dbus_timeout: float = DEFAULT_DBUS_TIMEOUT_SECONDS,
    ) -> None:
        self._bus = bus
        self._configured_adapter = _normalize_adapter(adapter) if adapter else None
        self._adapter_path: Optional[str] = None
        self._dbus_timeout = dbus_timeout

    @property
    def adapter_path(self) -> str | None:
        return self._adapter_path

    def find_adapter(self, adapter: str | None = None) -> str:
        """Resolve one configured adapter, refusing ambiguous multi-adapter hosts."""

        requested = _normalize_adapter(adapter) if adapter else self._configured_adapter
        objects = self._managed_objects()
        adapter_properties = {
            str(path): interfaces[BLUEZ_ADAPTER_IFACE]
            for path, interfaces in objects.items()
            if BLUEZ_ADAPTER_IFACE in interfaces
        }
        adapters = sorted(adapter_properties)
        if requested is not None:
            if _ADDRESS_RE.fullmatch(requested):
                matches = [
                    path
                    for path, properties in adapter_properties.items()
                    if str(properties.get("Address", "")).upper() == requested
                ]
                selected = matches[0] if len(matches) == 1 else None
            else:
                selected = requested if requested in adapters else None
            if selected is None:
                raise RuntimeError(
                    f"Configured Bluetooth adapter {requested} was not found; "
                    f"available: {adapters or 'none'}"
                )
        elif len(adapters) == 1:
            selected = adapters[0]
        elif not adapters:
            raise RuntimeError("No Bluetooth adapter found — is bluetoothd running?")
        else:
            raise RuntimeError(
                "Multiple Bluetooth adapters found; configure one explicitly: "
                + ", ".join(adapters)
            )
        self._adapter_path = selected
        log.info("Using Bluetooth adapter at %s", selected)
        return selected

    def set_powered(self, on: bool) -> None:
        self._set_adapter_property("Powered", dbus.Boolean(on))
        log.info("Adapter powered %s", "on" if on else "off")

    def set_connectable(self, on: bool) -> None:
        """Allow paired phones to reconnect without opening discovery or pairing."""

        try:
            self._set_adapter_property("Connectable", dbus.Boolean(on))
        except dbus.exceptions.DBusException as exc:
            if exc.get_dbus_name() != "org.freedesktop.DBus.Error.UnknownProperty":
                raise
            # Older BlueZ versions manage connectability through Powered and
            # do not expose this separate property.
            log.info("BlueZ does not expose a separate Connectable property")
            return
        log.info("Adapter connectable %s", "on" if on else "off")

    def set_pairable(self, on: bool, *, timeout_seconds: int | None = None) -> None:
        self._set_adapter_property("Pairable", dbus.Boolean(on))
        if timeout_seconds is not None:
            if timeout_seconds < 0:
                raise ValueError("Pairable timeout cannot be negative")
            self._set_adapter_property(
                "PairableTimeout", dbus.UInt32(timeout_seconds)
            )

    def get_paired_hfp_devices(self) -> list[dict]:
        """Return paired devices on the selected adapter advertising HFP AG."""

        adapter_path = self._require_adapter()
        results: list[dict] = []
        for path, interfaces in self._managed_objects().items():
            path_text = str(path)
            if not path_text.startswith(adapter_path + "/"):
                continue
            props = interfaces.get(BLUEZ_DEVICE_IFACE)
            if props is None or not bool(props.get("Paired", False)):
                continue
            uuids = {str(uuid).lower() for uuid in props.get("UUIDs", ())}
            if HFP_AG_UUID.lower() not in uuids:
                continue
            results.append(
                {
                    "address": str(props.get("Address", "")).upper(),
                    "name": str(
                        props.get("Alias")
                        or props.get("Name")
                        or "Unknown"
                    ),
                    "connected": bool(props.get("Connected", False)),
                    "paired": True,
                    "trusted": bool(props.get("Trusted", False)),
                    "services_resolved": bool(props.get("ServicesResolved", False)),
                    "path": path_text,
                }
            )
        return sorted(results, key=lambda device: device["address"])

    def connect_device(self, address: str) -> None:
        """Connect HFP explicitly, including when the base link is disconnected.

        Generic Device1.Connect can select another bearer/profile on a dual-mode
        phone and time out without starting HFP. ConnectProfile establishes the
        BR/EDR link needed by HFP and keeps unrelated profiles out of the request.
        """

        path, props = self._find_device(address)
        if not bool(props.get("Paired", False)):
            raise RuntimeError(f"Bluetooth device {address} is not paired")
        uuids = {str(uuid).lower() for uuid in props.get("UUIDs", ())}
        if HFP_AG_UUID.lower() not in uuids:
            raise RuntimeError(f"Bluetooth device {address} does not advertise HFP AG")
        device = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, path),
            BLUEZ_DEVICE_IFACE,
        )
        log.info("Connecting HFP profile to %s", address.upper())
        device.ConnectProfile(
            dbus.String(HFP_AG_UUID), timeout=self._dbus_timeout
        )

    def disconnect_device(self, address: str) -> None:
        """Disconnect only HFP, leaving unrelated Bluetooth profiles alone."""

        path, _ = self._find_device(address)
        device = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, path),
            BLUEZ_DEVICE_IFACE,
        )
        log.info("Disconnecting HFP profile from %s", address.upper())
        device.DisconnectProfile(
            dbus.String(HFP_AG_UUID), timeout=self._dbus_timeout
        )

    def get_device(self, address: str) -> dict:
        path, props = self._find_device(address)
        return {
            "path": path,
            "address": str(props.get("Address", "")).upper(),
            "paired": bool(props.get("Paired", False)),
            "connected": bool(props.get("Connected", False)),
            "services_resolved": bool(props.get("ServicesResolved", False)),
            "uuids": tuple(str(uuid).lower() for uuid in props.get("UUIDs", ())),
        }

    def _set_adapter_property(self, name: str, value) -> None:
        path = self._require_adapter()
        props = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, path),
            DBUS_PROPS_IFACE,
        )
        props.Set(BLUEZ_ADAPTER_IFACE, name, value)

    def _require_adapter(self) -> str:
        if self._adapter_path is None:
            raise RuntimeError("Call find_adapter() first")
        return self._adapter_path

    def _managed_objects(self):
        manager = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, "/"),
            DBUS_OM_IFACE,
        )
        return manager.GetManagedObjects(timeout=self._dbus_timeout)

    def _find_device(self, address: str):
        adapter_path = self._require_adapter()
        normalized = _normalize_address(address)
        for path, interfaces in self._managed_objects().items():
            path_text = str(path)
            if not path_text.startswith(adapter_path + "/"):
                continue
            props = interfaces.get(BLUEZ_DEVICE_IFACE)
            if props is None:
                continue
            if str(props.get("Address", "")).upper() == normalized:
                return path_text, props
        raise RuntimeError(
            f"Bluetooth device {normalized} was not found on {adapter_path}"
        )

    def _address_to_path(self, address: str) -> str:
        """Compatibility wrapper that now resolves ObjectManager exactly."""

        path, _ = self._find_device(address)
        return path


def _normalize_adapter(adapter: str) -> str:
    value = adapter.strip()
    address = value.upper().replace("-", ":")
    if _ADDRESS_RE.fullmatch(address):
        return address
    if value.startswith("/org/bluez/"):
        return value.rstrip("/")
    if not re.fullmatch(r"hci\d+", value):
        raise ValueError(
            "Bluetooth adapter must be hciN, a BlueZ object path, or an exact MAC "
            f"address: {adapter!r}"
        )
    return f"/org/bluez/{value}"


def _normalize_address(address: str) -> str:
    normalized = address.strip().upper().replace("-", ":")
    if not _ADDRESS_RE.fullmatch(normalized):
        raise ValueError(f"Invalid Bluetooth address: {address!r}")
    return normalized
