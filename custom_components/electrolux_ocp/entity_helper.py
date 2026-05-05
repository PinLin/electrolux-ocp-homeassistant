"""Shared platform-setup glue for per-appliance entities.

Each platform delegates entity construction to a callable that receives one
appliance dict and the coordinator, and returns the entities it owns for
that appliance. The helper handles two paths:

* initial sync — iterate ``coordinator.data.appliances`` once at platform
  setup and add entities for every appliance the coordinator has already
  fetched (covers the normal happy path after first refresh).
* dynamic discovery — subscribe to ``NEW_APPLIANCE_SIGNAL`` so appliances
  added to the account post-setup get their entities without scanning every
  WS push (which would otherwise fan out across every platform's listener).

Trade-off: this helper only fires once per *new* appliance, so a property
that appears in ``reported`` long after the appliance was first registered
will not auto-create an entity. For PURE A9 (the only confirmed device
line) the firmware reports every supported property from the moment it
connects, so the trade-off is fine. Revisit if a device line shows up
where reported keys grow over time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import NEW_APPLIANCE_SIGNAL
from .coordinator import ElectroluxDataUpdateCoordinator
from .models import ElectroluxConfigEntry


async def async_setup_appliance_entities(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build_entities_fn: Callable[
        [dict[str, Any], ElectroluxDataUpdateCoordinator], list[Entity]
    ],
) -> None:
    """Wire a platform up to per-appliance entity creation."""
    coordinator = entry.runtime_data.coordinator
    seen: set[str] = set()

    @callback
    def _add_for_appliance(appliance: dict[str, Any]) -> None:
        fresh: list[Entity] = []
        for entity in build_entities_fn(appliance, coordinator):
            uid = entity.unique_id
            if uid is None or uid in seen:
                continue
            seen.add(uid)
            fresh.append(entity)
        if fresh:
            async_add_entities(fresh)

    appliances = coordinator.data.appliances if coordinator.data else []
    for appliance in appliances:
        _add_for_appliance(appliance)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{NEW_APPLIANCE_SIGNAL}_{entry.entry_id}",
            _add_for_appliance,
        )
    )
