"""Shared runtime models for Electrolux."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .api import ElectroluxApiClient
    from .coordinator import ElectroluxDataUpdateCoordinator


@dataclass(slots=True, kw_only=True)
class ElectroluxData:
    """Snapshot returned by the coordinator each refresh."""

    appliances: list[dict[str, Any]] = field(default_factory=list)
    user: dict[str, Any] | None = None
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class ElectroluxRuntimeData:
    """Runtime objects attached to ConfigEntry.runtime_data."""

    client: ElectroluxApiClient
    coordinator: ElectroluxDataUpdateCoordinator


type ElectroluxConfigEntry = ConfigEntry[ElectroluxRuntimeData]
