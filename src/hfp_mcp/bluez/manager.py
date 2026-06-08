"""
BlueZManager — adapter discovery, device enumeration, connect/disconnect.

All methods are synchronous and blocking; callers in asyncio should use
  await loop.run_in_executor(None, manager.some_method, ...)
"""

from __future__ import annotations

import logging
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


class BlueZManager:
    def __init__(self, bus: dbus.SystemBus) -> None:
        self._bus = bus
        self._adapter_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Adapter
    # ------------------------------------------------------------------

    def find_adapter(self) -> str:
        """Locate the first Bluetooth adapter and cache its path."""
        om = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, "/"),
            DBUS_OM_IFACE,
        )
        objects = om.GetManagedObjects()
        for path, ifaces in objects.items():
            if BLUEZ_ADAPTER_IFACE in ifaces:
                self._adapter_path = str(path)
                log.info("Found adapter at %s", path)
                return str(path)
        raise RuntimeError("No Bluetooth adapter found — is bluetoothd running?")

    def set_powered(self, on: bool) -> None:
        if not self._adapter_path:
            raise RuntimeError("Call find_adapter() first")
        props = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
            DBUS_PROPS_IFACE,
        )
        props.Set(BLUEZ_ADAPTER_IFACE, "Powered", dbus.Boolean(on))
        log.info("Adapter powered %s", "on" if on else "off")

    def set_pairable(self, on: bool) -> None:
        if not self._adapter_path:
            raise RuntimeError("Call find_adapter() first")
        props = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
            DBUS_PROPS_IFACE,
        )
        props.Set(BLUEZ_ADAPTER_IFACE, "Pairable", dbus.Boolean(on))

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    def get_paired_hfp_devices(self) -> list[dict]:
        """
        Return paired devices that advertise the HFP Audio Gateway UUID.
        These are phones that can act as the cellular endpoint.
        """
        om = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, "/"),
            DBUS_OM_IFACE,
        )
        objects = om.GetManagedObjects()
        results: list[dict] = []
        for path, ifaces in objects.items():
            if BLUEZ_DEVICE_IFACE not in ifaces:
                continue
            props = ifaces[BLUEZ_DEVICE_IFACE]
            uuids = [str(u).lower() for u in props.get("UUIDs", [])]
            if HFP_AG_UUID in uuids:
                results.append(
                    {
                        "address": str(props.get("Address", "")),
                        "name": str(props.get("Name", "Unknown")),
                        "connected": bool(props.get("Connected", False)),
                        "paired": bool(props.get("Paired", False)),
                        "path": str(path),
                    }
                )
        return results

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def connect_device(self, address: str) -> None:
        """
        Tell BlueZ to connect to the device.  BlueZ will call our
        HFPProfile.NewConnection callback once the RFCOMM channel is open.
        """
        path = self._address_to_path(address)
        dev = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, path),
            BLUEZ_DEVICE_IFACE,
        )
        log.info("Connecting to %s …", address)
        dev.Connect()

    def disconnect_device(self, address: str) -> None:
        path = self._address_to_path(address)
        dev = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, path),
            BLUEZ_DEVICE_IFACE,
        )
        log.info("Disconnecting from %s …", address)
        dev.Disconnect()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _address_to_path(self, address: str) -> str:
        if not self._adapter_path:
            raise RuntimeError("Call find_adapter() first")
        mac = address.upper().replace(":", "_")
        return f"{self._adapter_path}/dev_{mac}"
