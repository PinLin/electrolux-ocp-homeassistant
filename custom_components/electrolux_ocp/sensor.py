"""Dynamic sensors for Electrolux appliances."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    extract_appliance_id,
    summarize_appliance,
)
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux sensors based on a config entry."""
    coordinator = entry.runtime_data.coordinator

    async_add_entities([
        ElectroluxAccountSensor(coordinator, entry.entry_id),
        ElectroluxTokenExpirySensor(coordinator, entry.entry_id),
        ElectroluxLastUpdateSensor(coordinator, entry.entry_id),
    ])

    await async_setup_appliance_entities(
        hass, entry, async_add_entities, _build_entities_for_appliance
    )


def _build_entities_for_appliance(
    appliance: dict[str, Any],
    coordinator: ElectroluxDataUpdateCoordinator,
) -> list[Entity]:
    """Build per-appliance sensor entities from coordinator capability data."""
    appliance_id = extract_appliance_id(appliance)
    if not appliance_id:
        return []

    entities: list[Entity] = []

    capabilities = coordinator.get_capabilities(appliance_id) or {}
    reported = appliance.get("properties", {}).get("reported", {})
    for key, cap in capabilities.items():
        if derive_platform(cap, key) != "sensor":
            continue
        # Hinted keys register eagerly; un-hinted keys wait for first report.
        if key not in PROPERTY_HINTS and key not in reported:
            continue
        entities.append(
            ElectroluxDynamicPropertySensor(coordinator, appliance_id, key)
        )

    return entities


class ElectroluxAccountSensor(
    CoordinatorEntity[ElectroluxDataUpdateCoordinator], SensorEntity
):
    """Account-level summary sensor (count of linked appliances)."""

    _attr_has_entity_name = True
    _attr_translation_key = "appliance_count"

    def __init__(
        self, coordinator: ElectroluxDataUpdateCoordinator, entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_account"
        self.entity_id = f"sensor.{DOMAIN}_appliances"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.appliances) if self.coordinator.data else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        user = data.user
        return {
            "account_email": user.get("email") if isinstance(user, Mapping) else None,
            "account_user_name": user.get("userName") if isinstance(user, Mapping) else None,
            "appliances": [summarize_appliance(appliance) for appliance in data.appliances],
        }


class ElectroluxTokenExpirySensor(
    CoordinatorEntity[ElectroluxDataUpdateCoordinator], SensorEntity
):
    """Diagnostic sensor exposing access-token expiry and last refresh result.

    Lets the user (or automations) notice when token rotation is misbehaving
    well before the integration falls over with cas_3412 / cas_3404.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "token_expiry"

    def __init__(
        self, coordinator: ElectroluxDataUpdateCoordinator, entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_token_expiry"
        self.entity_id = f"sensor.{DOMAIN}_token_expiry"

    @property
    def native_value(self) -> datetime | None:
        ts = self.coordinator.client.access_token_expires_at
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self.coordinator.client
        last_refresh = client.last_refresh_at
        return {
            "last_refresh_at": (
                datetime.fromtimestamp(last_refresh, tz=timezone.utc).isoformat()
                if last_refresh is not None
                else None
            ),
            "last_refresh_status": client.last_refresh_status,
        }


class ElectroluxLastUpdateSensor(
    CoordinatorEntity[ElectroluxDataUpdateCoordinator], SensorEntity
):
    """Timestamp of the most recent successful coordinator refresh.

    Useful for "data is stale" automations and for diagnosing whether
    polling is still alive when WS push events stop arriving.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "last_update"

    def __init__(
        self, coordinator: ElectroluxDataUpdateCoordinator, entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_last_update"
        self.entity_id = f"sensor.{DOMAIN}_last_update"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_success_at


class ElectroluxDynamicPropertySensor(ElectroluxBaseEntity, SensorEntity):
    """Sensor entity for any sensor-typed capability the appliance exposes."""

    _state_attrs = ("native_value",)

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
        appliance_id: str,
        property_key: str,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self._property_key = property_key
        self._attr_unique_id = f"{appliance_id}_{property_key.lower()}"
        # Force English entity_id so device names containing CJK don't get
        # pinyin-slugified when the appliance name contains CJK characters.
        self.entity_id = f"sensor.{DOMAIN}_{appliance_id}_{property_key.lower()}"

        capabilities = coordinator.get_capabilities(appliance_id) or {}
        cap = capabilities.get(property_key, {})
        attrs = derive_ha_attrs(cap, property_key)
        self._attr_translation_key = attrs.get("translation_key") or auto_translation_key(property_key)
        self._attr_device_class = attrs.get("device_class")
        self._attr_native_unit_of_measurement = attrs.get("native_unit_of_measurement")
        self._attr_state_class = attrs.get("state_class")
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]
        if attrs.get("disabled_by_default"):
            self._attr_entity_registry_enabled_default = False
        # Optional callable applied to the raw reported value before HA sees
        # it. Used to e.g. lowercase enum-like strings so they match the
        # lowercase translation keys hassfest demands.
        self._value_transform = attrs.get("value_transform")
        # Optional whitelist for enum-style sensors. Anything outside the
        # set gets a one-shot warning and is surfaced as "unknown" so the
        # frontend doesn't display an untranslated raw cloud value.
        self._known_values: frozenset[str] | None = attrs.get("known_values")
        self._warned_unknown_values: set[str] = set()

    @property
    def native_value(self) -> Any:
        appliance = self._appliance
        if not appliance:
            return None
        value = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        if value is None:
            return None
        if self._value_transform is not None:
            value = self._value_transform(value)
        if self._known_values is not None and isinstance(value, str):
            if value not in self._known_values:
                if value not in self._warned_unknown_values:
                    self._warned_unknown_values.add(value)
                    _LOGGER.warning(
                        "Unknown value %r for %s sensor on appliance %s; "
                        "reporting 'unknown'. Please open an issue so the "
                        "translation can be added.",
                        value,
                        self._property_key,
                        self._appliance_id,
                    )
                return "unknown"
        return value
