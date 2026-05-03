"""Pure-function tests for fan.py heuristics and capability derivation."""

from __future__ import annotations

from custom_components.electrolux.fan import (
    DEFAULT_PRESET_MODES,
    DEFAULT_SPEED_RANGE,
    _derive_preset_modes,
    _derive_speed_range,
    _looks_like_air_purifier,
)


class TestLooksLikeAirPurifier:
    def test_appliance_type_match(self):
        assert _looks_like_air_purifier({"applianceType": "AIRPURIFIER"}, {}) is True

    def test_appliance_type_partial_match(self):
        assert _looks_like_air_purifier({"applianceType": "Air Purifier"}, {}) is True

    def test_model_contains_pure(self):
        assert _looks_like_air_purifier({"model": "PUREA9"}, {}) is True

    def test_model_contains_air(self):
        assert _looks_like_air_purifier({"model": "AirCare 360"}, {}) is True

    def test_capability_fingerprint(self):
        # Fall-through: a future appliance with Workmode+Fanspeed capabilities
        # gets treated as a fan even without a matching type/model string.
        assert _looks_like_air_purifier({}, {"Workmode": {}, "Fanspeed": {}}) is True

    def test_capability_only_workmode_does_not_match(self):
        # One half of the fingerprint isn't enough — guards against e.g. an
        # appliance that has work modes but no fan speed.
        assert _looks_like_air_purifier({}, {"Workmode": {}}) is False

    def test_unrelated_appliance_rejected(self):
        # A dishwasher with neither match should not be exposed as a fan.
        assert _looks_like_air_purifier({"applianceType": "DISHWASHER"}, {}) is False

    def test_empty_inputs(self):
        assert _looks_like_air_purifier({}, {}) is False


class TestDeriveSpeedRange:
    def test_min_max_explicit(self):
        caps = {"Fanspeed": {"min": 1, "max": 5}}
        assert _derive_speed_range(caps) == (1, 5)

    def test_min_max_invalid_when_min_ge_max(self):
        # Defensive: garbage range falls through to values / default.
        caps = {"Fanspeed": {"min": 5, "max": 5}}
        assert _derive_speed_range(caps) == DEFAULT_SPEED_RANGE

    def test_values_dict_with_integer_keys(self):
        caps = {"Fanspeed": {"values": {"1": {}, "2": {}, "3": {}, "4": {}}}}
        assert _derive_speed_range(caps) == (1, 4)

    def test_values_dict_with_non_integer_keys_skipped(self):
        # Non-int keys silently dropped; remaining ints form the range.
        caps = {"Fanspeed": {"values": {"low": {}, "1": {}, "2": {}, "high": {}}}}
        assert _derive_speed_range(caps) == (1, 2)

    def test_default_when_no_capabilities(self):
        assert _derive_speed_range({}) == DEFAULT_SPEED_RANGE

    def test_default_when_capabilities_is_none(self):
        # Coordinator passes None when /capabilities 404s (PUREA9 case).
        assert _derive_speed_range(None) == DEFAULT_SPEED_RANGE


class TestDerivePresetModes:
    def test_preset_modes_from_workmode_values(self):
        caps = {"Workmode": {"values": {"Auto": {}, "Manual": {}, "Smart": {}, "PowerOff": {}}}}
        modes = _derive_preset_modes(caps)
        # PowerOff is filtered out; remaining order preserved.
        assert "PowerOff" not in modes
        assert set(modes) == {"Auto", "Manual", "Smart"}

    def test_default_when_no_workmode(self):
        assert _derive_preset_modes({}) == list(DEFAULT_PRESET_MODES)

    def test_default_when_only_poweroff_present(self):
        # Edge case: workmode lists only PowerOff → no usable presets, fall back.
        caps = {"Workmode": {"values": {"PowerOff": {}}}}
        assert _derive_preset_modes(caps) == list(DEFAULT_PRESET_MODES)

    def test_default_when_capabilities_is_none(self):
        assert _derive_preset_modes(None) == list(DEFAULT_PRESET_MODES)
