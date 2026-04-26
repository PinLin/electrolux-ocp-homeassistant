"""Contracts between PROPERTY_HINTS, derive_platform, RESERVED_PROPERTIES, and i18n.

These regression tests catch subtle drift after the entity layer became
capability-driven — e.g. someone adding a hint without a translation, or
moving Workmode out of RESERVED_PROPERTIES so it would suddenly appear as
a duplicate select alongside the fan entity.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.electrolux.capabilities import (
    PROPERTY_HINTS,
    PURE_A9_CAPABILITIES,
    RESERVED_PROPERTIES,
    derive_platform,
)


COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "electrolux"


def _load_translation(filename: str) -> dict:
    return json.loads((COMPONENT_ROOT / filename).read_text(encoding="utf-8"))


# Map a hint to the strings.json section its translation_key lives under.
# We use PURE_A9_CAPABILITIES as ground truth for the hint's intended
# platform because a few hints (like the writeable booleans) only have
# semantic meaning paired with an access flag.
def _platform_for_hint(key: str) -> str:
    cap = PURE_A9_CAPABILITIES.get(key)
    if cap is None:
        # Hint exists but no PURE A9 capability claims it (e.g. BatteryLevel,
        # not present on PURE A9 but kept around for future devices). Default
        # to "sensor" — the most permissive section.
        return "sensor"
    return derive_platform(cap, key) or "sensor"


class TestPropertyHintsStructure:
    def test_each_entry_has_translation_key(self):
        for key, cfg in PROPERTY_HINTS.items():
            assert "translation_key" in cfg, f"{key} missing translation_key"

    def test_translation_keys_match_strings_json(self):
        en = _load_translation("strings.json")
        sections = en.get("entity", {})
        for key, cfg in PROPERTY_HINTS.items():
            tk = cfg["translation_key"]
            section = _platform_for_hint(key)
            section_keys = set(sections.get(section, {}).keys())
            assert tk in section_keys, (
                f"{key} → {tk} missing from strings.json entity.{section}"
            )

    def test_translation_keys_match_zh_hant(self):
        zh = _load_translation("translations/zh-Hant.json")
        sections = zh.get("entity", {})
        for key, cfg in PROPERTY_HINTS.items():
            tk = cfg["translation_key"]
            section = _platform_for_hint(key)
            section_keys = set(sections.get(section, {}).keys())
            assert tk in section_keys, (
                f"{key} → {tk} missing from zh-Hant.json entity.{section}"
            )


class TestPlatformAssignments:
    """Verify derive_platform routes PURE A9 properties to the platforms that
    historically owned them, so the refactor didn't silently move entities
    between platforms."""

    def test_writeable_booleans_become_switches(self):
        for key in ("UILight", "SafetyLock", "Ionizer"):
            cap = PURE_A9_CAPABILITIES[key]
            assert derive_platform(cap, key) == "switch", f"{key} should be switch"

    def test_door_and_error_flags_become_binary_sensors(self):
        for key in (
            "DoorOpen",
            "ErrPM2_5",
            "ErrTVOC",
            "ErrTempHumidity",
            "ErrFanMtr",
            "ErrCommSensorDisplayBrd",
            "ErrRFID",
        ):
            cap = PURE_A9_CAPABILITIES[key]
            assert derive_platform(cap, key) == "binary_sensor", (
                f"{key} should be binary_sensor"
            )

    def test_numeric_measurements_become_sensors(self):
        for key in (
            "Temp",
            "Humidity",
            "PM1",
            "PM2_5",
            "PM10",
            "ECO2",
            "TVOC",
            "FilterLife",
            "RSSI",
        ):
            cap = PURE_A9_CAPABILITIES[key]
            assert derive_platform(cap, key) == "sensor", f"{key} should be sensor"

    def test_signal_strength_string_is_sensor(self):
        # Read-only string with no enum values → falls through to sensor.
        cap = PURE_A9_CAPABILITIES["SignalStrength"]
        assert derive_platform(cap, "SignalStrength") == "sensor"

    def test_internal_diagnostics_are_hidden(self):
        for key in ("logE", "logW", "TVOCBrand"):
            cap = PURE_A9_CAPABILITIES[key]
            assert derive_platform(cap, key) is None, f"{key} should be hidden"


class TestReservedProperties:
    """Properties claimed by domain-specific platforms must not leak into
    the generic discovery in sensor/binary_sensor/switch/select."""

    def test_fan_owned_properties_are_reserved(self):
        # fan.py builds the air-purifier entity around Workmode + Fanspeed;
        # raw service diagnostics are also intentionally hidden.
        assert "Workmode" in RESERVED_PROPERTIES
        assert "Fanspeed" in RESERVED_PROPERTIES
        assert "logE" in RESERVED_PROPERTIES
        assert "logW" in RESERVED_PROPERTIES
        assert "TVOCBrand" in RESERVED_PROPERTIES

    def test_reserved_properties_return_none_from_derive_platform(self):
        for key in RESERVED_PROPERTIES:
            cap = PURE_A9_CAPABILITIES.get(key, {"access": "readwrite", "type": "string"})
            assert derive_platform(cap, key) is None, (
                f"{key} is reserved; derive_platform must return None"
            )
