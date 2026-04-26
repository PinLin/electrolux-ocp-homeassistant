"""Select entities for Electrolux appliances.

Exposes any writeable string capability that carries a ``values`` enum as
an HA select entity. Reserved properties (``Workmode``, ``Fanspeed``) are
explicitly skipped so they don't duplicate the fan entity's preset_mode.

OCP values are sent through the same command shape as switches: a single
``{property_key: chosen_value}`` payload via ``async_send_command``.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    """Set up Electrolux selects based on a config entry."""
    coordinator = entry.runtime_data.coordinator
    seen_entities: set[str] = set()

    @callback
    def async_add_new_entities() -> None:
        new_entities: list[SelectEntity] = []
        appliances = coordinator.data.appliances if coordinator.data else []
        for appliance in appliances:
            appliance_id = extract_appliance_id(appliance)
            if not appliance_id:
                continue

            capabilities = coordinator.get_capabilities(appliance_id) or {}
            reported = appliance.get("properties", {}).get("reported", {})

            for key, cap in capabilities.items():
                if derive_platform(cap, key) != "select":
                    continue
                if key not in reported:
                    continue

                uid = f"{entry.entry_id}_{appliance_id}_{key.lower()}"
                if uid not in seen_entities:
                    seen_entities.add(uid)
                    new_entities.append(
                        ElectroluxSelect(coordinator, appliance_id, key)
                    )

        if new_entities:
            async_add_entities(new_entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class ElectroluxSelect(ElectroluxBaseEntity, SelectEntity):
    """Select for any writeable string capability with a values enum."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        property_key: str,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self._property_key = property_key
        self._attr_unique_id = f"{appliance_id}_{property_key.lower()}"
        self.entity_id = f"select.{DOMAIN}_{appliance_id}_{property_key.lower()}"

        capabilities = coordinator.get_capabilities(appliance_id) or {}
        cap = capabilities.get(property_key, {})
        attrs = derive_ha_attrs(cap, property_key)
        self._attr_translation_key = attrs.get("translation_key") or auto_translation_key(property_key)
        if "icon" in attrs:
            self._attr_icon = attrs["icon"]
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]

        # Cloud values are case-sensitive enum keys (PascalCase, etc.). HA
        # surfaces them as-is; if downstream cards or scripts complain we
        # can revisit by adding a per-key transform in PROPERTY_HINTS.
        values = cap.get("values") or {}
        self._attr_options = list(values.keys())

    @property
    def current_option(self) -> str | None:
        appliance = self._appliance
        if not appliance:
            return None
        value = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        if value is None:
            return None
        return str(value)

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        await client.async_send_command(self._appliance_id, {self._property_key: option})
        await self.coordinator.async_request_refresh()
