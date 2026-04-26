"""Tests for the capabilities provider chain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.electrolux.capabilities import (
    PROPERTY_HINTS,
    PURE_A9_CAPABILITIES,
    RESERVED_PROPERTIES,
    ChainCapabilitiesProvider,
    OcpV2CapabilitiesProvider,
    StaticPureA9Provider,
    build_default_provider,
    derive_ha_attrs,
    derive_platform,
)


PURE_A9_APPLIANCE = {
    "applianceId": "950011538_test",
    "applianceData": {"applianceName": "Living room", "modelName": "PURE A9"},
    "applianceType": "AIRPURIFIER",
    "properties": {"reported": {}},
}

OVEN_APPLIANCE = {
    "applianceId": "914550543_oven",
    "applianceData": {"applianceName": "Kitchen oven", "modelName": "Oven 9000"},
    "applianceType": "OVEN",
    "properties": {"reported": {}},
}


@pytest.mark.asyncio
async def test_static_pure_a9_provider_matches_air_purifier():
    provider = StaticPureA9Provider()
    caps = await provider.async_fetch(PURE_A9_APPLIANCE)
    assert caps is not None
    # Sanity-check the contract that fan.py and the entity layer rely on.
    assert caps["Workmode"]["values"] == {"Auto": {}, "Manual": {}, "PowerOff": {}}
    assert caps["Fanspeed"]["min"] == 1
    assert caps["Fanspeed"]["max"] == 9
    assert caps["UILight"]["access"] == "readwrite"
    assert caps["DoorOpen"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_static_pure_a9_provider_skips_other_devices():
    provider = StaticPureA9Provider()
    assert await provider.async_fetch(OVEN_APPLIANCE) is None


@pytest.mark.asyncio
async def test_static_pure_a9_provider_returns_copy():
    provider = StaticPureA9Provider()
    caps = await provider.async_fetch(PURE_A9_APPLIANCE)
    caps["Fanspeed"]["max"] = 99
    # Subsequent calls must not see the mutation; the module-level constant
    # is the source of truth.
    fresh = await provider.async_fetch(PURE_A9_APPLIANCE)
    assert fresh["Fanspeed"]["max"] == PURE_A9_CAPABILITIES["Fanspeed"]["max"]


@pytest.mark.asyncio
async def test_ocp_v2_provider_returns_dict_when_available():
    client = MagicMock()
    client.async_get_capabilities = AsyncMock(
        return_value={"Workmode": {"access": "readwrite", "type": "string"}}
    )
    provider = OcpV2CapabilitiesProvider(client)
    caps = await provider.async_fetch(PURE_A9_APPLIANCE)
    assert caps == {"Workmode": {"access": "readwrite", "type": "string"}}


@pytest.mark.asyncio
async def test_ocp_v2_provider_returns_none_on_404():
    from custom_components.electrolux.api import ElectroluxApiError

    client = MagicMock()
    client.async_get_capabilities = AsyncMock(side_effect=ElectroluxApiError("404: not found"))
    provider = OcpV2CapabilitiesProvider(client)
    assert await provider.async_fetch(PURE_A9_APPLIANCE) is None


@pytest.mark.asyncio
async def test_ocp_v2_provider_returns_none_on_empty_response():
    client = MagicMock()
    client.async_get_capabilities = AsyncMock(return_value={})
    provider = OcpV2CapabilitiesProvider(client)
    assert await provider.async_fetch(PURE_A9_APPLIANCE) is None


@pytest.mark.asyncio
async def test_chain_falls_through_to_static_when_ocp_404():
    from custom_components.electrolux.api import ElectroluxApiError

    client = MagicMock()
    client.async_get_capabilities = AsyncMock(side_effect=ElectroluxApiError("404"))
    chain = ChainCapabilitiesProvider(
        [OcpV2CapabilitiesProvider(client), StaticPureA9Provider()]
    )
    caps = await chain.async_fetch(PURE_A9_APPLIANCE)
    # Static provider supplied the schema; PURE A9 keys present.
    assert caps is not None
    assert "Fanspeed" in caps


@pytest.mark.asyncio
async def test_chain_prefers_ocp_response_when_available():
    client = MagicMock()
    client.async_get_capabilities = AsyncMock(
        return_value={"Custom": {"access": "read", "type": "int"}}
    )
    chain = ChainCapabilitiesProvider(
        [OcpV2CapabilitiesProvider(client), StaticPureA9Provider()]
    )
    caps = await chain.async_fetch(PURE_A9_APPLIANCE)
    assert caps == {"Custom": {"access": "read", "type": "int"}}


@pytest.mark.asyncio
async def test_chain_returns_none_when_no_provider_matches():
    from custom_components.electrolux.api import ElectroluxApiError

    client = MagicMock()
    client.async_get_capabilities = AsyncMock(side_effect=ElectroluxApiError("404"))
    chain = ChainCapabilitiesProvider(
        [OcpV2CapabilitiesProvider(client), StaticPureA9Provider()]
    )
    assert await chain.async_fetch(OVEN_APPLIANCE) is None


@pytest.mark.asyncio
async def test_build_default_provider_returns_chain_with_both_providers():
    client = MagicMock()
    provider = build_default_provider(client)
    assert isinstance(provider, ChainCapabilitiesProvider)
    # Two providers wired in the documented order.
    assert len(provider._providers) == 2
    assert isinstance(provider._providers[0], OcpV2CapabilitiesProvider)
    assert isinstance(provider._providers[1], StaticPureA9Provider)


# ---- derive_platform ----------------------------------------------------


def test_derive_platform_writeable_boolean_is_switch():
    assert derive_platform({"access": "readwrite", "type": "boolean"}, "X") == "switch"
    assert derive_platform({"access": "write", "type": "boolean"}, "X") == "switch"


def test_derive_platform_readonly_boolean_is_binary_sensor():
    assert derive_platform({"access": "read", "type": "boolean"}, "X") == "binary_sensor"


def test_derive_platform_numeric_is_sensor():
    assert derive_platform({"access": "read", "type": "int"}, "X") == "sensor"
    assert derive_platform({"access": "readwrite", "type": "number"}, "X") == "sensor"


def test_derive_platform_writeable_string_with_values_is_select():
    cap = {"access": "readwrite", "type": "string", "values": {"A": {}, "B": {}}}
    assert derive_platform(cap, "X") == "select"


def test_derive_platform_string_without_values_is_sensor():
    # PURE A9 SignalStrength is read-only enum-shaped without values listed.
    assert derive_platform({"access": "read", "type": "string"}, "X") == "sensor"


def test_derive_platform_skips_reserved_properties():
    # Reserved properties are either owned by a domain-specific platform or
    # deliberately hidden from generic discovery.
    assert derive_platform({"access": "readwrite", "type": "string"}, "Workmode") is None
    assert derive_platform({"access": "readwrite", "type": "int"}, "Fanspeed") is None
    assert derive_platform({"access": "read", "type": "int"}, "logE") is None
    assert derive_platform({"access": "read", "type": "int"}, "logW") is None
    assert derive_platform({"access": "read", "type": "string"}, "TVOCBrand") is None
    assert "Workmode" in RESERVED_PROPERTIES
    assert "Fanspeed" in RESERVED_PROPERTIES
    assert "logE" in RESERVED_PROPERTIES
    assert "logW" in RESERVED_PROPERTIES
    assert "TVOCBrand" in RESERVED_PROPERTIES


def test_derive_platform_skips_nested_keys():
    # Washing-machine capabilities use ``parent/child`` paths that flat HA
    # entities can't address — must return None until we model them.
    cap = {"access": "readwrite", "type": "boolean"}
    assert derive_platform(cap, "applianceCareAndMaintenance0/maint1_occured") is None


def test_derive_platform_skips_alert_and_complex_types():
    assert derive_platform({"access": "read", "type": "alert"}, "X") is None
    assert derive_platform({"access": "read", "type": "complex"}, "X") is None
    # careMaintenance and other unknowns also fall through.
    assert derive_platform({"access": "read", "type": "careMaintenance"}, "X") is None


def test_derive_platform_handles_missing_access_as_read_only():
    # Defensive: real cloud responses occasionally omit access; treat as read.
    assert derive_platform({"type": "boolean"}, "X") == "binary_sensor"


# ---- derive_ha_attrs ----------------------------------------------------


def test_derive_ha_attrs_known_sensor_property():
    attrs = derive_ha_attrs({"access": "read", "type": "int"}, "Temp")
    assert attrs["device_class"] is SensorDeviceClass.TEMPERATURE
    assert attrs["translation_key"] == "temperature"
    assert "native_unit_of_measurement" in attrs


def test_derive_ha_attrs_known_binary_sensor_property():
    attrs = derive_ha_attrs({"access": "read", "type": "boolean"}, "DoorOpen")
    assert attrs["device_class"] is BinarySensorDeviceClass.PROBLEM
    assert attrs["translation_key"] == "filter_cover_open"


def test_derive_ha_attrs_known_switch_property():
    attrs = derive_ha_attrs({"access": "readwrite", "type": "boolean"}, "UILight")
    assert attrs["translation_key"] == "ui_light"


def test_derive_ha_attrs_returns_empty_dict_for_unknown_key():
    # No PROPERTY_HINTS entry — entity layer falls back to auto-generated name.
    assert derive_ha_attrs({"access": "read", "type": "int"}, "TotallyNewMetric") == {}


def test_derive_ha_attrs_returns_fresh_copy():
    # Mutating the returned dict must not poison subsequent lookups.
    attrs = derive_ha_attrs({"access": "read", "type": "int"}, "Temp")
    attrs["device_class"] = "MUTATED"
    assert derive_ha_attrs({"access": "read", "type": "int"}, "Temp")["device_class"] is SensorDeviceClass.TEMPERATURE


def test_property_hints_covers_existing_pure_a9_schema():
    # Every PURE A9 property that previously had a hint must still have one
    # so the entity-layer refactor doesn't silently drop niceties.
    expected = {
        # numeric measurements
        "Temp", "Humidity", "PM1", "PM2_5", "PM10", "ECO2", "TVOC",
        "FilterLife", "RSSI", "BatteryLevel",
        # string measurement
        "SignalStrength",
        # error flags
        "DoorOpen", "ErrPM2_5", "ErrTVOC", "ErrTempHumidity", "ErrFanMtr",
        "ErrCommSensorDisplayBrd", "ErrRFID",
        # writeable booleans
        "UILight", "SafetyLock", "Ionizer",
    }
    missing = expected - PROPERTY_HINTS.keys()
    assert not missing, f"PROPERTY_HINTS lost coverage for: {missing}"


# ---- StaticPureA9Provider identification --------------------------------


@pytest.mark.asyncio
async def test_static_provider_matches_canonical_modelname():
    # The federation probe confirmed PURE A9 reports modelName="PUREA9".
    appliance = {
        "applianceId": "x",
        "applianceData": {"modelName": "PUREA9"},
        "properties": {"reported": {}},
    }
    caps = await StaticPureA9Provider().async_fetch(appliance)
    assert caps is not None and "Fanspeed" in caps


@pytest.mark.asyncio
async def test_static_provider_matches_canonical_appliance_type():
    # The /appliances/info endpoint returns deviceType="AIR_PURIFIER" — when
    # that ever surfaces in the appliance dict (e.g. via a future enrichment
    # provider), match on it directly without falling back to substrings.
    appliance = {
        "applianceId": "x",
        "applianceType": "AIR_PURIFIER",
        "properties": {"reported": {}},
    }
    caps = await StaticPureA9Provider().async_fetch(appliance)
    assert caps is not None


@pytest.mark.asyncio
async def test_static_provider_falls_back_to_substring_heuristic():
    # An unenumerated air-purifier branding should still match via the
    # last-resort heuristic, so AEG/sub-brand variants work out of the box.
    appliance = {
        "applianceId": "x",
        "applianceData": {"modelName": "Some Air Purifier 9000"},
        "properties": {"reported": {}},
    }
    caps = await StaticPureA9Provider().async_fetch(appliance)
    assert caps is not None


@pytest.mark.asyncio
async def test_static_provider_skips_completely_unrelated_devices():
    appliance = {
        "applianceId": "x",
        "applianceData": {"modelName": "WM_DELUXE"},
        "applianceType": "WASHING_MACHINE",
        "properties": {"reported": {}},
    }
    assert await StaticPureA9Provider().async_fetch(appliance) is None
