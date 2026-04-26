"""Diagnostics support for Electrolux."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import REDACT_KEYS
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
