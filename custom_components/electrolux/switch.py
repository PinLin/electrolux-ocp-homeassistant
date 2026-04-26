"""Switch entities for Electrolux appliances."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import extract_appliance_id
from .capabilities import auto_translation_key, derive_ha_attrs, derive_platform
from .const import DOMAIN
from .entity import ElectroluxBaseEntity
from .models import ElectroluxConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux switches based on a config entry."""
    coordinator = entry.runtime_data.coordinator
    seen_entities: set[str] = set()

    @callback
    def async_add_new_entities() -> None:
        new_entities: list[SwitchEntity] = []
        appliances = coordinator.data.appliances if coordinator.data else []
        for appliance in appliances:
            appliance_id = extract_appliance_id(appliance)
            if not appliance_id:
                continue

            capabilities = coordinator.get_capabilities(appliance_id) or {}
            reported = appliance.get("properties", {}).get("reported", {})

            for key, cap in capabilities.items():
                if derive_platform(cap, key) != "switch":
                    continue
                # Only register a switch once the appliance has actually
                # reported the property at least once. Avoids creating
                # phantom entities for capabilities the device declares
                # but never broadcasts (seen on partially-provisioned
                # appliances).
                if key not in reported:
                    continue

                uid = f"{entry.entry_id}_{appliance_id}_{key.lower()}"
                if uid not in seen_entities:
                    seen_entities.add(uid)
                    new_entities.append(
                        ElectroluxSwitch(coordinator, appliance_id, key)
                    )

        if new_entities:
            async_add_entities(new_entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class ElectroluxSwitch(ElectroluxBaseEntity, SwitchEntity):
    """Switch for any writeable boolean capability the appliance exposes."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        property_key: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, appliance_id)
        self._property_key = property_key
        self._attr_unique_id = f"{appliance_id}_{property_key.lower()}"
        self.entity_id = f"switch.{DOMAIN}_{appliance_id}_{property_key.lower()}"

        capabilities = coordinator.get_capabilities(appliance_id) or {}
        cap = capabilities.get(property_key, {})
        attrs = derive_ha_attrs(cap, property_key)
        self._attr_translation_key = attrs.get("translation_key") or auto_translation_key(property_key)
        if "icon" in attrs:
            self._attr_icon = attrs["icon"]
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        appliance = self._appliance
        if not appliance:
            return None
        return appliance.get("properties", {}).get("reported", {}).get(self._property_key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._send_command(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._send_command(False)

    async def _send_command(self, value: bool) -> None:
        """Send a command to the appliance."""
        client = self.coordinator.client
        await client.async_send_command(self._appliance_id, {self._property_key: value})
        await self.coordinator.async_request_refresh()
