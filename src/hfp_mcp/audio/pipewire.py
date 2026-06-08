"""
Locate PipeWire audio nodes created by BlueZ when an HFP SCO link comes up.

When an HFP call becomes active, BlueZ (via the bluez5 PipeWire module or
WirePlumber) creates source/sink nodes named approximately:

  bluez_input.AA_BB_CC_DD_EE_FF.0    ← phone mic / incoming audio
  bluez_output.AA_BB_CC_DD_EE_FF.0   ← phone speaker / outgoing audio

We use `pactl list short sources/sinks` (PulseAudio compat layer provided
by pipewire-pulse) to find them.  The nodes only exist while the SCO link
is up, so we poll with a timeout.
"""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)


class PipeWireDeviceLocator:
    def find_hfp_devices(self, bt_address: str) -> tuple[str | None, str | None]:
        """
        Return (source_name, sink_name) for the HFP SCO nodes of bt_address.
        Returns (None, None) if the nodes don't exist yet.
        """
        addr_variants = _address_variants(bt_address)
        source = _find_node("sources", addr_variants)
        sink = _find_node("sinks", addr_variants)
        return source, sink

    def wait_for_hfp_devices(
        self, bt_address: str, timeout: float = 10.0
    ) -> tuple[str | None, str | None]:
        """
        Poll until PipeWire creates the HFP audio nodes or timeout expires.
        SCO nodes appear only after the call transitions to ACTIVE.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            source, sink = self.find_hfp_devices(bt_address)
            if source and sink:
                log.info("HFP audio nodes: source=%s  sink=%s", source, sink)
                return source, sink
            time.sleep(0.5)
        log.warning("HFP audio nodes not found for %s within %.1fs", bt_address, timeout)
        return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _address_variants(bt_address: str) -> list[str]:
    """Generate the address forms that may appear in PipeWire node names."""
    return [
        bt_address.upper().replace(":", "_"),
        bt_address.lower().replace(":", "_"),
        bt_address.upper().replace(":", ""),
        bt_address.lower().replace(":", ""),
    ]


def _find_node(kind: str, addr_variants: list[str]) -> str | None:
    """
    kind: "sources" or "sinks"
    Returns the PipeWire node name or None.
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "short", kind],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            for av in addr_variants:
                if av.lower() in lower and ("hfp" in lower or "bluez" in lower):
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1]
    except FileNotFoundError:
        log.warning("pactl not found — is pipewire-pulse installed?")
    except subprocess.TimeoutExpired:
        log.warning("pactl timed out")
    except Exception as exc:
        log.warning("pactl error: %s", exc)
    return None
