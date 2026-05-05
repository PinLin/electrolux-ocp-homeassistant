"""Base entity for Electrolux integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    extract_appliance_manufacturer,
    extract_appliance_model,
    extract_appliance_model_id,
    extract_appliance_name,
    extract_appliance_serial,
    extract_firmware_version,
)
from .const import DOMAIN
from .coordinator import ElectroluxDataUpdateCoordinator


class ElectroluxBaseEntity(CoordinatorEntity[ElectroluxDataUpdateCoordinator]):
    """Base class for Electrolux entities."""

    _attr_has_entity_name = True

    # Subclasses list the entity property names that, when changed, require
    # broadcasting a new state to HA. Each WS push that lands in coordinator
    # data fans out to every entity's _handle_coordinator_update; this guard
    # suppresses async_write_ha_state when the snapshot of those properties
    # is unchanged. An empty tuple keeps the default CoordinatorEntity
    # behaviour (broadcast on every refresh).
    _state_attrs: tuple[str, ...] = ()

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
        appliance_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._last_broadcast_state: tuple[Any, ...] | None = None

    @property
    def _appliance(self) -> Mapping[str, Any] | None:
        """Return the appliance data from the coordinator (O(1) lookup)."""
        return self.coordinator.get_appliance(self._appliance_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information about this appliance."""
        appliance = self._appliance
        if appliance is None:
            return DeviceInfo(identifiers={(DOMAIN, self._appliance_id)})

        reported = appliance.get("properties", {}).get("reported", {}) if isinstance(appliance, Mapping) else {}
        appliance_fw = extract_firmware_version(appliance)
        sw_version = appliance_fw or reported.get("FrmVer_NIU")

        return DeviceInfo(
            identifiers={(DOMAIN, self._appliance_id)},
            manufacturer=extract_appliance_manufacturer(appliance),
            name=extract_appliance_name(appliance),
            model=extract_appliance_model(appliance),
            model_id=extract_appliance_model_id(appliance),
            serial_number=extract_appliance_serial(appliance),
            sw_version=sw_version,
        )

    async def async_added_to_hass(self) -> None:
        """Seed the last-broadcast snapshot so the first real update is honest."""
        await super().async_added_to_hass()
        self._refresh_last_broadcast()

    def _refresh_last_broadcast(self) -> bool:
        """Recompute the snapshot. Return True if it differs from the prior one."""
        if not self._state_attrs:
            return True
        snapshot = tuple(getattr(self, attr, None) for attr in self._state_attrs)
        if snapshot != self._last_broadcast_state:
            self._last_broadcast_state = snapshot
            return True
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Only broadcast when the entity's tracked state actually changed."""
        if self._refresh_last_broadcast():
            self.async_write_ha_state()
