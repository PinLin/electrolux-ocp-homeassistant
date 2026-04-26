"""Tests for capability-driven switch discovery and naming."""

from __future__ import annotations

from custom_components.electrolux.capabilities import (
    auto_translation_key as _auto_translation_key,
)


class TestAutoTranslationKey:
    """Translation key auto-generation for capabilities without PROPERTY_HINTS."""

    def test_pascalcase_to_snake_case(self):
        assert _auto_translation_key("UILight") == "ui_light"
        assert _auto_translation_key("SafetyLock") == "safety_lock"
        assert _auto_translation_key("Ionizer") == "ionizer"

    def test_acronym_handling(self):
        # Consecutive caps should not produce empty segments — the regex
        # uses negative-lookbehind for [A-Z_] to keep "PM2_5" intact and
        # "DspIcoPM2_5" splittable.
        assert _auto_translation_key("DspIcoPM2_5") == "dsp_ico_pm2_5"
        assert _auto_translation_key("PM2_5") == "pm2_5"

    def test_already_snake_case_passthrough(self):
        # Pure lowercase + underscore stays as-is once lowercased.
        assert _auto_translation_key("filter_life") == "filter_life"

    def test_hassfest_compatible_charset(self):
        # Hassfest enforces ``[a-z0-9_-]+``. Verify on a representative
        # cross-section that the auto-generator never emits other chars.
        import re
        valid = re.compile(r"^[a-z0-9_-]+$")
        for prop in (
            "UILight", "SafetyLock", "DspIcoPM2_5", "ErrCommSensorDisplayBrd",
            "FilterRFID", "applianceState", "endOfCycleSound",
        ):
            tk = _auto_translation_key(prop)
            assert valid.match(tk), f"{prop!r} produced invalid key {tk!r}"
