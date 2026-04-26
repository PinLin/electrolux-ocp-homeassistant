"""Base entity for Electrolux integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

    def __init__(
        self,
        coordinator: ElectroluxDataUpdateCoordinator,
        appliance_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._appliance_id = appliance_id

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
