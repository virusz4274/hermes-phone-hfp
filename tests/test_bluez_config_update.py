"""Tests for the installer BlueZ main.conf updater."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_HELPER_PATH = Path(__file__).resolve().parents[1] / "setup" / "update_bluez_main_conf.py"
_SPEC = importlib.util.spec_from_file_location("update_bluez_main_conf", _HELPER_PATH)
update_bluez_main_conf = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(update_bluez_main_conf)


def test_update_content_adds_autoenable_without_experimental():
    content = "[General]\nName = BlueZ\n\n[Policy]\n#AutoEnable=false\n"

    updated = update_bluez_main_conf.update_content(content)

    assert "[Policy]\nAutoEnable=true" in updated
    assert "Experimental=true" not in updated


def test_update_content_removes_legacy_duplicate_experimental_section():
    content = (
        "[General]\n"
        "Name = BlueZ\n"
        "\n"
        "[Policy]\n"
        "AutoEnable=true\n"
        "\n"
        "[AdvMon]\n"
        "#RSSISamplingPeriod=0xFF\n"
        "\n"
        "[General]\n"
        "Experimental=true\n"
    )

    updated = update_bluez_main_conf.update_content(content)

    assert updated.count("[General]") == 1
    assert "Name = BlueZ" in updated
    assert "Experimental=true" not in updated
    assert "[AdvMon]" in updated


def test_update_content_preserves_intentional_experimental_in_primary_general():
    content = "[General]\nExperimental=true\n\n[Policy]\n"

    updated = update_bluez_main_conf.update_content(content)

    assert "Experimental=true" in updated
    assert "AutoEnable=true" in updated
