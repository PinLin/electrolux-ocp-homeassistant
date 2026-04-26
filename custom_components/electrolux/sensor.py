"""Dynamic sensors for Electrolux appliances."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    extract_appliance_id,
    extract_state_value,
    summarize_appliance,
)
from .capabilities import auto_translation_key, derive_ha_attrs, derive_platform
from .const import DOMAIN
from .entity import ElectroluxBaseEntity
from .models import ElectroluxConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux sensors based on a config entry."""
    coordinator = entry.runtime_data.coordinator
    seen_entities: set[str] = set()

    async_add_entities([
        ElectroluxAccountSensor(coordinator, entry.entry_id),
        ElectroluxTokenExpirySensor(coordinator, entry.entry_id),
        ElectroluxLastUpdateSensor(coordinator, entry.entry_id),
    ])

    @callback
    def async_add_new_entities() -> None:
        new_entities: list[SensorEntity] = []
        appliances = coordinator.data.appliances if coordinator.data else []
        for appliance in appliances:
            appliance_id = extract_appliance_id(appliance)
            if not appliance_id:
                continue

            # Connection state diagnostic sensor — always present, not
            # capability-driven (this isn't a property of the device, it's
            # the cloud's view of the device's reachability).
            uid = f"{entry.entry_id}_{appliance_id}_state"
            if uid not in seen_entities:
                seen_entities.add(uid)
                new_entities.append(ElectroluxApplianceStateSensor(coordinator, appliance_id))

            capabilities = coordinator.get_capabilities(appliance_id) or {}
            reported = appliance.get("properties", {}).get("reported", {})

            for key, cap in capabilities.items():
                if derive_platform(cap, key) != "sensor":
                    continue
                if key not in reported:
                    continue

                uid = f"{entry.entry_id}_{appliance_id}_{key.lower()}"
                if uid not in seen_entities:
                    seen_entities.add(uid)
                    new_entities.append(
                        ElectroluxDynamicPropertySensor(coordinator, appliance_id, key)
                    )

        if new_entities:
            async_add_entities(new_entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class ElectroluxAccountSensor(CoordinatorEntity, SensorEntity):
    """Account-level summary sensor (count of linked appliances)."""

    _attr_has_entity_name = True
    _attr_translation_key = "appliance_count"
    _attr_icon = "mdi:home-analytics"

    def __init__(self, coordinator, entry_id: str) -> None:
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


class ElectroluxTokenExpirySensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor exposing access-token expiry and last refresh result.

    Lets the user (or automations) notice when token rotation is misbehaving
    well before the integration falls over with cas_3412 / cas_3404.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:key-chain"
    _attr_has_entity_name = True
    _attr_translation_key = "token_expiry"

    def __init__(self, coordinator, entry_id: str) -> None:
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


class ElectroluxLastUpdateSensor(CoordinatorEntity, SensorEntity):
    """Timestamp of the most recent successful coordinator refresh.

    Useful for "data is stale" automations and for diagnosing whether
    polling is still alive when WS push events stop arriving.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"
    _attr_has_entity_name = True
    _attr_translation_key = "last_update"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_last_update"
        self.entity_id = f"sensor.{DOMAIN}_last_update"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_success_at


class ElectroluxApplianceStateSensor(ElectroluxBaseEntity, SensorEntity):
    """Connection state diagnostic sensor for an appliance."""

    _attr_icon = "mdi:information-outline"
    _attr_translation_key = "connection_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, appliance_id: str) -> None:
        super().__init__(coordinator, appliance_id)
        self._attr_unique_id = f"{appliance_id}_connection_state"
        self.entity_id = f"sensor.{DOMAIN}_{appliance_id}_connection_state"

    @property
    def native_value(self) -> str | None:
        appliance = self._appliance
        if not appliance:
            return None
        return extract_state_value(appliance)


class ElectroluxDynamicPropertySensor(ElectroluxBaseEntity, SensorEntity):
    """Sensor entity for any sensor-typed capability the appliance exposes."""

    def __init__(self, coordinator, appliance_id: str, property_key: str) -> None:
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
        if "icon" in attrs:
            self._attr_icon = attrs["icon"]
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]
        # Optional callable applied to the raw reported value before HA sees
        # it. Used to e.g. lowercase enum-like strings so they match the
        # lowercase translation keys hassfest demands.
        self._value_transform = attrs.get("value_transform")

    @property
    def native_value(self) -> Any:
        appliance = self._appliance
        if not appliance:
            return None
        value = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        if value is None or self._value_transform is None:
            return value
        return self._value_transform(value)
