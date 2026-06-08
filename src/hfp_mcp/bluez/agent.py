"""
HFPAgent — D-Bus object implementing org.bluez.Agent1.

Handles Bluetooth pairing in headless mode.
Capability "DisplayYesNo" — Pi auto-accepts RequestConfirmation while the
phone displays the passkey for the user to confirm on the phone side.
"""

from __future__ import annotations

import logging

import dbus
import dbus.service

from ..config import (
    AGENT_PATH,
    BLUEZ_AGENT_MANAGER_IFACE,
    BLUEZ_PROFILE_MANAGER_PATH,
    BLUEZ_SERVICE,
)

log = logging.getLogger(__name__)

AGENT1_IFACE = "org.bluez.Agent1"


class HFPAgent(dbus.service.Object):
    """Implements org.bluez.Agent1 — Pi auto-accepts, phone shows passkey to user."""

    def __init__(self, bus: dbus.SystemBus) -> None:
        super().__init__(bus, AGENT_PATH)

    @dbus.service.method(AGENT1_IFACE, in_signature="", out_signature="")
    def Release(self):  # noqa: N802
        pass

    @dbus.service.method(AGENT1_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):  # noqa: N802
        log.info("Pairing confirmation requested from %s (passkey=%06d)", device, passkey)
        # Auto-accept — headless Pi has no display to show passkey

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):  # noqa: N802
        log.info("Authorization requested from %s — accepted", device)

    @dbus.service.method(AGENT1_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):  # noqa: N802
        log.info("AuthorizeService from %s uuid=%s — accepted", device, uuid)

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):  # noqa: N802
        log.info("RequestPinCode from %s", device)
        return "0000"

    @dbus.service.method(AGENT1_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):  # noqa: N802
        log.info("RequestPasskey from %s", device)
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
