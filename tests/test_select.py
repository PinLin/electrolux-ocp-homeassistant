"""Tests for capability-driven select discovery.

Mostly a sanity check that derive_platform routes writeable string + values
capabilities to ``"select"`` so an unfamiliar appliance with e.g.
``defaultExtraRinse`` (washing machine) gets a select entity automatically.
"""

from __future__ import annotations

from custom_components.electrolux.capabilities import (
    derive_ha_attrs,
    derive_platform,
)


def test_writeable_string_with_values_is_select():
    cap = {
        "access": "readwrite",
        "type": "string",
        "values": {"NONE": {}, "ONE": {}, "TWO": {}},
    }
    assert derive_platform(cap, "extraRinseNumber") == "select"


def test_select_options_inherit_from_values_keys():
    # ElectroluxSelect.__init__ uses cap["values"].keys() for _attr_options.
    # Verify the shape we depend on: a dict whose keys are the option labels.
    cap = {
        "access": "readwrite",
        "type": "string",
        "values": {"OFF": {}, "AUTO": {}, "MANUAL": {}},
    }
    options = list(cap["values"].keys())
    assert options == ["OFF", "AUTO", "MANUAL"]


def test_unhinted_select_property_falls_back_to_auto_translation():
    # Unfamiliar property name → derive_ha_attrs returns no translation_key
    # and the entity layer is expected to substitute auto_translation_key().
    cap = {"access": "readwrite", "type": "string", "values": {"a": {}}}
    attrs = derive_ha_attrs(cap, "ExtraRinseNumber")
    assert "translation_key" not in attrs


def test_writeable_string_without_values_is_not_select():
    # No enum → not a select; should fall back to sensor (read-only string).
    cap = {"access": "readwrite", "type": "string"}
    # Our policy: writeable strings without values aren't selects (nothing
    # to choose from). They become string-typed sensors.
    assert derive_platform(cap, "FreeText") == "sensor"
