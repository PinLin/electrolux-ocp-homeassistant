"""Capability schema and providers.

OCP exposes property capability documents at
``/appliance/api/v2/appliances/{id}/capabilities``, but the endpoint returns
404 for every air purifier in the WellbeingA9/Muju line: the OneApp Android
app builds those screens from hard-coded Kotlin model enums, not from a
runtime schema. The integration therefore needs more than one source of
capability data and a way to compose them.

A ``CapabilitiesProvider`` returns a ``CapabilityDict`` for a given appliance,
or ``None`` if the provider has no opinion. The coordinator chains providers
in priority order and takes the first non-None answer:

    OcpV2CapabilitiesProvider   ->  works for any device OCP exposes
    StaticPureA9Provider        ->  hand-curated schema for WellbeingA9 / Muju
    (future) DeveloperApiProvider, OcpFederationProvider, ...

The shape of a ``Capability`` mirrors the OCP v2 response (cross-checked
against ``py-electrolux-ocp`` typed dicts and a real WM sample), so a future
provider that wraps the developer API at ``api.developer.electrolux.one``
can yield directly into the same dict without a translation layer.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Protocol, TypedDict

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_BILLION,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.helpers.entity import EntityCategory

from .api import (
    ElectroluxApiClient,
    ElectroluxApiError,
    extract_appliance_id,
    extract_appliance_model,
    extract_appliance_type,
)

_LOGGER = logging.getLogger(__name__)


class Capability(TypedDict, total=False):
    """Single property capability entry, OCP-shaped."""

    access: Literal["read", "write", "readwrite"]
    type: Literal["boolean", "int", "number", "string", "enum", "alert", "complex"]
    values: dict[str, Any]
    min: float
    max: float
    step: float


CapabilityDict = dict[str, Capability]


# HA-specific overlay applied to every capability entry regardless of source.
# Property names match the OCP-reported keys verbatim. Unknown properties get
# auto-named (PascalCase -> "Title Case") and no device_class — still better
# than not appearing at all on a device the integration has never met.
#
# Migrated from sensor.SENSOR_MAP / binary_sensor.binary_mapping /
# switch.switchable_keys so the three platforms can share a single source of
# truth and so a future device that happens to report `Temp` or `Humidity`
# inherits the right HA niceties for free.
PROPERTY_HINTS: dict[str, dict[str, Any]] = {
    # ---- numeric measurements (sensor) ----
    "Temp": {
        "translation_key": "temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "Humidity": {
        "translation_key": "humidity",
        "device_class": SensorDeviceClass.HUMIDITY,
        "native_unit_of_measurement": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "PM1": {
        "translation_key": "pm1",
        "device_class": SensorDeviceClass.PM1,
        "native_unit_of_measurement": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "PM2_5": {
        "translation_key": "pm2_5",
        "device_class": SensorDeviceClass.PM25,
        "native_unit_of_measurement": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "PM10": {
        "translation_key": "pm10",
        "device_class": SensorDeviceClass.PM10,
        "native_unit_of_measurement": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "ECO2": {
        "translation_key": "eco2",
        "device_class": SensorDeviceClass.CO2,
        "native_unit_of_measurement": CONCENTRATION_PARTS_PER_MILLION,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "TVOC": {
        "translation_key": "tvoc",
        "native_unit_of_measurement": CONCENTRATION_PARTS_PER_BILLION,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "FilterLife": {
        "translation_key": "filter_life",
        "native_unit_of_measurement": PERCENTAGE,
        "icon": "mdi:filter-outline",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "RSSI": {
        "translation_key": "rssi",
        "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
        "native_unit_of_measurement": "dBm",
        "state_class": SensorStateClass.MEASUREMENT,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    "BatteryLevel": {
        "translation_key": "battery_level",
        "device_class": SensorDeviceClass.BATTERY,
        "native_unit_of_measurement": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    # ---- string measurements (sensor) ----
    "SignalStrength": {
        "translation_key": "signal_quality",
        "icon": "mdi:wifi",
        "entity_category": EntityCategory.DIAGNOSTIC,
        # NIU firmware reports "EXCELLENT", "GOOD", etc. Lowercased so it
        # matches the lowercase translation keys hassfest demands.
        "value_transform": str.lower,
    },
    # ---- read-only boolean flags (binary_sensor) ----
    "DoorOpen": {
        "translation_key": "filter_cover_open",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrPM2_5": {
        "translation_key": "pm2_5_sensor_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrTVOC": {
        "translation_key": "tvoc_sensor_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrTempHumidity": {
        "translation_key": "temp_humidity_sensor_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrFanMtr": {
        "translation_key": "fan_motor_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrCommSensorDisplayBrd": {
        "translation_key": "communication_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "ErrRFID": {
        "translation_key": "filter_rfid_error",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    # ---- writeable booleans (switch) ----
    "UILight": {"translation_key": "ui_light"},
    "SafetyLock": {"translation_key": "safety_lock"},
    "Ionizer": {"translation_key": "ionizer"},
}


# Properties intentionally hidden from generic discovery. Some are claimed by
# domain-specific platforms (fan.py owns Workmode + Fanspeed); others are raw
# service counters or component metadata that are not useful as HA entities.
RESERVED_PROPERTIES: frozenset[str] = frozenset(
    {"Workmode", "Fanspeed", "logE", "logW", "TVOCBrand"}
)


def derive_platform(cap: Capability, key: str) -> str | None:
    """Return the HA platform a capability entry should map to, or None.

    Returning None means the entry is intentionally not surfaced — either
    claimed by a domain-specific platform (RESERVED_PROPERTIES), nested
    under a slash path that the entity layer doesn't model yet, or carrying
    a capability ``type`` that doesn't fit a flat HA entity (alerts,
    careMaintenance complexes, etc.).
    """
    if key in RESERVED_PROPERTIES:
        return None
    if "/" in key:
        # OCP nests deeper schemas under "parent/child" keys (washing-machine
        # ``applianceCareAndMaintenance0/maint1_*`` etc.). Flat HA entities
        # cannot express that without a unique_id rework — defer.
        return None

    cap_type = cap.get("type")
    access = cap.get("access", "read")
    writeable = access in ("write", "readwrite")

    if cap_type == "boolean":
        return "switch" if writeable else "binary_sensor"
    if cap_type in ("int", "number"):
        return "sensor"
    if cap_type == "string":
        if writeable and cap.get("values"):
            return "select"
        return "sensor"
    # alert / complex / careMaintenance / unknown — no generic mapping.
    return None


def derive_ha_attrs(cap: Capability, key: str) -> dict[str, Any]:
    """Return a fresh dict of HA entity attribute hints for ``key``.

    PROPERTY_HINTS is the primary source. When a property is not hinted
    (i.e. an unknown appliance reports a brand-new property name) we apply
    a few common-sense auto-classifications so the entity still surfaces
    sensibly without a code change.
    """
    hints = PROPERTY_HINTS.get(key)
    if hints:
        return dict(hints)

    # Fall-through inferences for un-hinted keys. Kept conservative — only
    # high-signal naming patterns. Anything ambiguous gets no device_class
    # and falls back to auto_translation_key for the entity name.
    out: dict[str, Any] = {}
    if key.startswith("Err") and cap.get("type") == "boolean":
        # OCP error/fault flags on every appliance line we've seen follow
        # the ``Err<Name>`` convention. PROBLEM surfaces them in the HA UI.
        out["device_class"] = BinarySensorDeviceClass.PROBLEM
    return out


def auto_translation_key(property_key: str) -> str:
    """Convert a PascalCase OCP property name to a snake_case translation key.

    Hassfest enforces ``[a-z0-9_-]+`` for translation keys; un-hinted
    properties on previously-unseen appliances need *something*. Falling
    back to a snake_case form is friendlier than dropping translation
    support entirely.

    Handles acronym boundaries (``UILight`` → ``ui_light``,
    ``DspIcoPM2_5`` → ``dsp_ico_pm2_5``) by splitting twice: first before
    any ``[A-Z][a-z]+`` run that's preceded by another character, then
    again at lower-or-digit-to-upper boundaries.
    """
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", property_key)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


class CapabilitiesProvider(Protocol):
    """Resolve the capability schema for a single appliance."""

    async def async_fetch(self, appliance: dict[str, Any]) -> CapabilityDict | None:
        """Return capabilities for ``appliance``, or None if not applicable."""


class OcpV2CapabilitiesProvider:
    """Calls ``/appliance/api/v2/appliances/{id}/capabilities``.

    Returns ``None`` on 404 (which is the expected outcome for the entire
    air-purifier line — see module docstring) and on transient API errors so
    the chain can move to the next provider.
    """

    def __init__(self, client: ElectroluxApiClient) -> None:
        self._client = client

    async def async_fetch(self, appliance: dict[str, Any]) -> CapabilityDict | None:
        appliance_id = extract_appliance_id(appliance)
        if not appliance_id:
            return None
        try:
            caps = await self._client.async_get_capabilities(appliance_id)
        except ElectroluxApiError as err:
            _LOGGER.debug("OCP v2 capabilities unavailable for %s: %s", appliance_id, err)
            return None
        if not isinstance(caps, dict) or not caps:
            return None
        return caps


# Hand-curated schema for the Electrolux/AEG WellbeingA9 / Muju air-purifier
# line. OneApp embeds the equivalent knowledge in Kotlin (jadx shows model
# enums j9.H.WellbeingA9 / WellbeingMuju with hard-coded UI bindings), so
# there is no cloud endpoint to derive this from. Property names match what
# OCP reports under ``properties.reported``; access flags match the commands
# the integration is known to send. Keep the keys identical to what the
# device reports so a future probe-derived provider can drop in unchanged.
PURE_A9_CAPABILITIES: CapabilityDict = {
    "Workmode": {
        "access": "readwrite",
        "type": "string",
        "values": {"Auto": {}, "Manual": {}, "PowerOff": {}},
    },
    "Fanspeed": {
        "access": "readwrite",
        "type": "int",
        "min": 1,
        "max": 9,
        "step": 1,
    },
    "UILight": {"access": "readwrite", "type": "boolean"},
    "SafetyLock": {"access": "readwrite", "type": "boolean"},
    "Ionizer": {"access": "readwrite", "type": "boolean"},
    "Temp": {"access": "read", "type": "int"},
    "Humidity": {"access": "read", "type": "int"},
    "PM1": {"access": "read", "type": "int"},
    "PM2_5": {"access": "read", "type": "int"},
    "PM10": {"access": "read", "type": "int"},
    "ECO2": {"access": "read", "type": "int"},
    "TVOC": {"access": "read", "type": "int"},
    "FilterLife": {"access": "read", "type": "int"},
    "RSSI": {"access": "read", "type": "int"},
    "SignalStrength": {"access": "read", "type": "string"},
    "BatteryLevel": {"access": "read", "type": "int"},
    "DoorOpen": {"access": "read", "type": "boolean"},
    "ErrPM2_5": {"access": "read", "type": "boolean"},
    "ErrTVOC": {"access": "read", "type": "boolean"},
    "ErrTempHumidity": {"access": "read", "type": "boolean"},
    "ErrFanMtr": {"access": "read", "type": "boolean"},
    "ErrCommSensorDisplayBrd": {"access": "read", "type": "boolean"},
    "ErrRFID": {"access": "read", "type": "boolean"},
    # Diagnostic counters and metadata observed in the federation probe.
    # Kept in the schema for completeness, but RESERVED_PROPERTIES prevents
    # them from surfacing as HA entities.
    "logE": {"access": "read", "type": "int"},
    "logW": {"access": "read", "type": "int"},
    "TVOCBrand": {"access": "read", "type": "string"},
}


# Canonical OCP modelName values for the WellbeingA9 / Muju air-purifier
# line. Confirmed for "PUREA9" via the federation endpoint probe; the
# smaller variants are listed for forward coverage based on jadx-extracted
# OneApp model enums (j9.H.Wellbeing*). Extend as new modelNames surface.
PURE_A9_MODEL_NAMES: frozenset[str] = frozenset({
    "PUREA9",
    "PUREMUJU",   # Muju compact variant
    "PUREASPEN",  # Aspen variant (per APK drawables)
    "WELLBEINGA9",
})


def _looks_like_air_purifier(appliance: dict[str, Any]) -> bool:
    """Decide whether an appliance is a WellbeingA9-family air purifier.

    Prefers canonical ``applianceData.modelName`` from the OCP payload
    (matched case-insensitively against PURE_A9_MODEL_NAMES); falls back
    to substring heuristics on appliance type / model name for variants
    we haven't enumerated yet.
    """
    appliance_type = (extract_appliance_type(appliance) or "").upper()
    model = (extract_appliance_model(appliance) or "").upper()

    if model in PURE_A9_MODEL_NAMES:
        return True
    if appliance_type in {"AIRPURIFIER", "AIR_PURIFIER"}:
        return True

    # Last-resort heuristic: catches modelName variants we haven't seen
    # (e.g. AEG-branded purifiers reusing the same firmware).
    return (
        "AIRPURIFIER" in appliance_type
        or "PURIFIER" in appliance_type
        or "PURE" in model
        or "AIR" in model
    )


class StaticPureA9Provider:
    """Returns ``PURE_A9_CAPABILITIES`` for any appliance that looks like a
    WellbeingA9/Muju air purifier.

    The Fanspeed range here matches PURE A9 (1..9). The smaller Muju unit
    actually tops out at 5 in normal mode — refine once a Muju appliance
    payload is available to disambiguate.
    """

    async def async_fetch(self, appliance: dict[str, Any]) -> CapabilityDict | None:
        if not _looks_like_air_purifier(appliance):
            return None
        return dict(PURE_A9_CAPABILITIES)


class ChainCapabilitiesProvider:
    """Tries providers in order; first non-empty result wins."""

    def __init__(self, providers: list[CapabilitiesProvider]) -> None:
        self._providers = providers

    async def async_fetch(self, appliance: dict[str, Any]) -> CapabilityDict | None:
        for provider in self._providers:
            result = await provider.async_fetch(appliance)
            if result:
                return result
        return None


def build_default_provider(client: ElectroluxApiClient) -> CapabilitiesProvider:
    """Construct the default provider chain for the integration."""
    return ChainCapabilitiesProvider(
        [
            OcpV2CapabilitiesProvider(client),
            StaticPureA9Provider(),
        ]
    )
