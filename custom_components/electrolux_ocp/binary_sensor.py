"""Binary sensor entities for Electrolux appliances."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from typing import Any

from .api import extract_appliance_id
from .capabilities import (
    PROPERTY_HINTS,
    auto_translation_key,
    derive_ha_attrs,
    derive_platform,
)
from .const import DOMAIN
from .coordinator import ElectroluxDataUpdateCoordinator
from .entity import ElectroluxBaseEntity
from .entity_helper import async_setup_appliance_entities
from .models import ElectroluxConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

_TRUE_STRS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRS = frozenset({"false", "0", "no", "off"})


def _coerce_bool(value: Any) -> bool | None:
    """Coerce a cloud-reported value into a strict bool, or None if unparseable.

    OCP normally sends JSON booleans, but we've seen string variants on a
    handful of error flags after firmware updates. Returning None for
    genuinely unrecognised values lets HA mark the entity unavailable
    rather than silently treating a string as truthy.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_STRS:
            return True
        if v in _FALSE_STRS:
            return False
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux binary sensors based on a config entry."""
    await async_setup_appliance_entities(
        hass, entry, async_add_entities, _build_entities_for_appliance
    )


def _build_entities_for_appliance(
    appliance: dict[str, Any],
    coordinator: ElectroluxDataUpdateCoordinator,
) -> list[Entity]:
    """Build per-appliance binary_sensor entities from capability data."""
    appliance_id = extract_appliance_id(appliance)
    if not appliance_id:
        return []
    entities: list[Entity] = []
    capabilities = coordinator.get_capabilities(appliance_id) or {}
    reported = appliance.get("properties", {}).get("reported", {})
    for key, cap in capabilities.items():
        if derive_platform(cap, key) != "binary_sensor":
            continue
        # Hinted keys are hand-curated and known to belong to the appliance
        # line, so we register them eagerly even before they appear in
        # reported state — partial provisioning shouldn't hide a known
        # PROBLEM flag. Un-hinted keys wait for the first report to avoid
        # phantom entities for capabilities a device declares but never
        # broadcasts.
        if key not in PROPERTY_HINTS and key not in reported:
            continue
        entities.append(ElectroluxBinarySensor(coordinator, appliance_id, key))
    return entities


class ElectroluxBinarySensor(ElectroluxBaseEntity, BinarySensorEntity):
    """Binary sensor for any read-only boolean capability the appliance exposes."""

    _state_attrs = ("is_on",)

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
        appliance_id: str,
        property_key: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, appliance_id)
        self._property_key = property_key
        self._attr_unique_id = f"{appliance_id}_{property_key.lower()}"
        self.entity_id = f"binary_sensor.{DOMAIN}_{appliance_id}_{property_key.lower()}"

        capabilities = coordinator.get_capabilities(appliance_id) or {}
        cap = capabilities.get(property_key, {})
        attrs = derive_ha_attrs(cap, property_key)
        self._attr_translation_key = attrs.get("translation_key") or auto_translation_key(property_key)
        if "device_class" in attrs:
            self._attr_device_class = attrs["device_class"]
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]
        self._warned_unknown_values: set[str] = set()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        appliance = self._appliance
        if not appliance:
            return None
        raw = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        coerced = _coerce_bool(raw)
        if coerced is None and raw is not None:
            key = repr(raw)
            if key not in self._warned_unknown_values:
                self._warned_unknown_values.add(key)
                _LOGGER.warning(
                    "Unparseable value %r for %s on appliance %s; reporting unavailable",
                    raw,
                    self._property_key,
                    self._appliance_id,
                )
        return coerced
