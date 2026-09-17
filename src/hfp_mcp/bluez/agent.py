"""
HFPAgent — D-Bus object implementing org.bluez.Agent1.

Handles Bluetooth pairing in headless mode.
Capability "DisplayYesNo" — Pi auto-accepts RequestConfirmation while the
phone displays the passkey for the user to confirm on the phone side.
"""

from __future__ import annotations

import logging
from typing import Callable

import dbus
import dbus.service

from ..config import (
    AGENT_PATH,
    BLUEZ_AGENT_MANAGER_IFACE,
    BLUEZ_PROFILE_MANAGER_PATH,
    BLUEZ_SERVICE,
    HFP_AG_UUID,
    HFP_HF_UUID,
)
from ..enrollment import address_from_bluez_path, authorize_enrollment_address

log = logging.getLogger(__name__)

AGENT1_IFACE = "org.bluez.Agent1"


class HFPAgentRejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class HFPAgent(dbus.service.Object):
    """Implements org.bluez.Agent1 — Pi auto-accepts, phone shows passkey to user."""

    def __init__(
        self,
        bus: dbus.SystemBus,
        *,
        configured_address: str | None = None,
        authorize: Callable[[str, str | None], bool] | None = None,
    ) -> None:
        super().__init__(bus, AGENT_PATH)
        self._configured_address = (
            configured_address.strip().upper() if configured_address else None
        )
        self._authorize = authorize

    def _require_authorized(self, device, service_uuid: str | None = None) -> str:
        try:
            address = address_from_bluez_path(str(device))
        except ValueError as exc:
            raise HFPAgentRejected("invalid Bluetooth device") from exc
        if service_uuid and service_uuid.lower() not in {HFP_HF_UUID, HFP_AG_UUID}:
            raise HFPAgentRejected("only the HFP service is authorized")
        allowed = (
            self._authorize(address, service_uuid)
            if self._authorize is not None
            else authorize_enrollment_address(address) or bool(
                self._configured_address
                and address == self._configured_address
            )
        )
        if not allowed:
            log.warning("Rejected Bluetooth authorization from %s", address)
            raise HFPAgentRejected("Bluetooth enrollment is closed")
        return address

    @dbus.service.method(AGENT1_IFACE, in_signature="", out_signature="")
    def Release(self):  # noqa: N802
        pass

    @dbus.service.method(AGENT1_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):  # noqa: N802
        address = self._require_authorized(device)
        log.info("Pairing confirmation requested from %s (passkey=%06d)", address, passkey)
        # Auto-accept — headless Pi has no display to show passkey

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):  # noqa: N802
        address = self._require_authorized(device)
        log.info("Authorization requested from %s — accepted", address)

    @dbus.service.method(AGENT1_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):  # noqa: N802
        address = self._require_authorized(device, str(uuid))
        log.info("AuthorizeService from %s uuid=%s — accepted", address, uuid)

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):  # noqa: N802
        address = self._require_authorized(device)
        log.info("RequestPinCode from %s", address)
        return "0000"

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):  # noqa: N802
        address = self._require_authorized(device)
        log.info("RequestPasskey from %s", address)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT1_IFACE, in_signature="", out_signature="")
    def Cancel(self):  # noqa: N802
        log.info("Pairing cancelled by BlueZ")


def register_agent(bus: dbus.SystemBus) -> None:
    """Register the agent with BlueZ and make it the default."""
    am = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, BLUEZ_PROFILE_MANAGER_PATH),
        BLUEZ_AGENT_MANAGER_IFACE,
    )
    am.RegisterAgent(AGENT_PATH, "DisplayYesNo")
    am.RequestDefaultAgent(AGENT_PATH)
    log.info("Pairing agent registered (DisplayYesNo)")


def unregister_agent(bus: dbus.SystemBus) -> None:
    """Release the Agent1 registration during an orderly daemon shutdown."""
    am = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, BLUEZ_PROFILE_MANAGER_PATH),
        BLUEZ_AGENT_MANAGER_IFACE,
    )
    am.UnregisterAgent(AGENT_PATH)
