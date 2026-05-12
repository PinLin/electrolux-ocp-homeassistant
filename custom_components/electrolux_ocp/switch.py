"""Switch entities for Electrolux appliances."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux switches based on a config entry."""
    await async_setup_appliance_entities(
        hass, entry, async_add_entities, _build_entities_for_appliance
    )


def _build_entities_for_appliance(
    appliance: dict[str, Any],
    coordinator: ElectroluxDataUpdateCoordinator,
) -> list[Entity]:
    """Build per-appliance switch entities from capability data."""
    appliance_id = extract_appliance_id(appliance)
    if not appliance_id:
        return []
    entities: list[Entity] = []
    capabilities = coordinator.get_capabilities(appliance_id) or {}
    reported = appliance.get("properties", {}).get("reported", {})
    for key, cap in capabilities.items():
        if derive_platform(cap, key) != "switch":
            continue
        # Hinted keys (PROPERTY_HINTS) are hand-curated and registered
        # eagerly — a partial provisioning gap shouldn't hide a known
        # control. Un-hinted keys wait for first report to avoid phantom
        # entities for capabilities a device declares but never broadcasts.
        if key not in PROPERTY_HINTS and key not in reported:
            continue
        entities.append(ElectroluxSwitch(coordinator, appliance_id, key))
    return entities


class ElectroluxSwitch(ElectroluxBaseEntity, SwitchEntity):
    """Switch for any writeable boolean capability the appliance exposes."""

    _state_attrs = ("is_on",)

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
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
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        appliance = self._appliance
        if not appliance:
            return None
        value = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._send_command(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._send_command(False)

    async def _send_command(self, value: bool) -> None:
        """Send a command to the appliance.

        Optimistic flow: paint the desired value into coordinator data
        *before* the PUT so the UI updates instantly. The previous design
        called ``async_request_refresh()`` immediately after PUT, which
        races against the cloud's eventually-consistent reported state and
        produced the "I toggled it on but it flips back to off" symptom.
        WS push delivers the real device-confirmed value; if the command
        fails outright we drop the optimistic state and re-poll.
        """
        payload = {self._property_key: value}
        self.coordinator.apply_optimistic_update(self._appliance_id, payload)
        try:
            await self.coordinator.client.async_send_command(self._appliance_id, payload)
        except Exception:
            self.coordinator.clear_optimistic_update(self._appliance_id, payload.keys())
            await self.coordinator.async_request_refresh()
            raise
