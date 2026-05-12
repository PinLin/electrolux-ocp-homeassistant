"""Select entities for Electrolux appliances.

Exposes any writeable string capability that carries a ``values`` enum as
an HA select entity. Reserved properties (``Workmode``, ``Fanspeed``) are
explicitly skipped so they don't duplicate the fan entity's preset_mode.

OCP values are sent through the same command shape as switches: a single
``{property_key: chosen_value}`` payload via ``async_send_command``.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux selects based on a config entry."""
    await async_setup_appliance_entities(
        hass, entry, async_add_entities, _build_entities_for_appliance
    )


def _build_entities_for_appliance(
    appliance: dict[str, Any],
    coordinator: ElectroluxDataUpdateCoordinator,
) -> list[Entity]:
    """Build per-appliance select entities from capability data."""
    appliance_id = extract_appliance_id(appliance)
    if not appliance_id:
        return []
    entities: list[Entity] = []
    capabilities = coordinator.get_capabilities(appliance_id) or {}
    reported = appliance.get("properties", {}).get("reported", {})
    for key, cap in capabilities.items():
        if derive_platform(cap, key) != "select":
            continue
        # Hinted keys register eagerly; un-hinted keys wait for first report.
        if key not in PROPERTY_HINTS and key not in reported:
            continue
        entities.append(ElectroluxSelect(coordinator, appliance_id, key))
    return entities


class ElectroluxSelect(ElectroluxBaseEntity, SelectEntity):
    """Select for any writeable string capability with a values enum."""

    _state_attrs = ("current_option",)

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
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
        if "entity_category" in attrs:
            self._attr_entity_category = attrs["entity_category"]

        # Cloud values are case-sensitive enum keys (PascalCase, etc.). HA
        # surfaces them as-is; if downstream cards or scripts complain we
        # can revisit by adding a per-key transform in PROPERTY_HINTS.
        values = cap.get("values") or {}
        self._attr_options = list(values.keys())
        self._warned_unknown_values: set[str] = set()

    @property
    def current_option(self) -> str | None:
        appliance = self._appliance
        if not appliance:
            return None
        value = appliance.get("properties", {}).get("reported", {}).get(self._property_key)
        if value is None:
            return None
        option = str(value)
        # HA logs an "Invalid option" warning every state read if we return a
        # value that's not in self._attr_options. Guard once, log once,
        # surface as None so the entity goes unavailable rather than spamming.
        if self._attr_options and option not in self._attr_options:
            if option not in self._warned_unknown_values:
                self._warned_unknown_values.add(option)
                _LOGGER.warning(
                    "Unknown option %r for %s on appliance %s; not in declared "
                    "options %s. Please open an issue.",
                    option,
                    self._property_key,
                    self._appliance_id,
                    self._attr_options,
                )
            return None
        return option

    async def async_select_option(self, option: str) -> None:
        # See switch._send_command for why we paint the value locally first
        # instead of refreshing immediately after PUT.
        payload = {self._property_key: option}
        self.coordinator.apply_optimistic_update(self._appliance_id, payload)
        try:
            await self.coordinator.client.async_send_command(self._appliance_id, payload)
        except Exception:
            self.coordinator.clear_optimistic_update(self._appliance_id, payload.keys())
            await self.coordinator.async_request_refresh()
            raise
