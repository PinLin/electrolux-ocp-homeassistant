"""Diagnostics support for Electrolux."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN, REDACT_KEYS
from .models import ElectroluxConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElectroluxConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    coordinator_data = runtime.coordinator.data
    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_KEYS),
        "coordinator": (
            async_redact_data(asdict(coordinator_data), REDACT_KEYS)
            if coordinator_data is not None
            else None
        ),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return diagnostics for a single appliance device.

    Includes the appliance payload, its capabilities, and a roster of
    entities registered for the device with their current state and
    attributes — the minimum to triage "this entity shows wrong value"
    bug reports without asking the user for screenshots.
    """
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    coordinator_data = coordinator.data

    # Resolve appliance_id from the device's identifiers; a device created by
    # this integration always has exactly one (DOMAIN, appliance_id) tuple.
    appliance_id: str | None = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            appliance_id = identifier
            break

    appliance: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    if appliance_id and coordinator_data is not None:
        appliance = coordinator.get_appliance(appliance_id)
        capabilities = coordinator_data.capabilities.get(appliance_id)

    # Walk the entity registry for entities tied to this device and snapshot
    # their current state.
    ent_reg = er.async_get(hass)
    entities: list[dict[str, Any]] = []
    for ent in er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
        state = hass.states.get(ent.entity_id)
        entities.append(
            {
                "entity_id": ent.entity_id,
                "unique_id": ent.unique_id,
                "platform": ent.platform,
                "domain": ent.domain,
                "translation_key": ent.translation_key,
                "device_class": ent.device_class or ent.original_device_class,
                "disabled_by": ent.disabled_by,
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else None,
            }
        )

    return {
        "device": {
            "id": device.id,
            "name": device.name,
            "name_by_user": device.name_by_user,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "model_id": device.model_id,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "identifiers": [list(i) for i in device.identifiers],
        },
        "appliance_id": appliance_id,
        "appliance": (
            async_redact_data(appliance, REDACT_KEYS) if appliance is not None else None
        ),
        "capabilities": capabilities,
        "entities": async_redact_data(entities, REDACT_KEYS),
    }
