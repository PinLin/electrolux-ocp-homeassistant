"""Fan entity for Electrolux air purifiers."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    int_states_in_range,
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .api import extract_appliance_id, extract_appliance_model, extract_appliance_type
from .const import DOMAIN
from .entity import ElectroluxBaseEntity
from .models import ElectroluxConfigEntry

# Conservative defaults used when /capabilities returns 404 (the case for
# PURE A9 air purifiers — OCP simply doesn't expose a capability document
# for them on the v2 endpoint, replying ocp_000600). These values were
# verified empirically against the OneApp reverse engineering.
DEFAULT_SPEED_RANGE = (1, 9)
DEFAULT_PRESET_MODES = ("Auto", "Manual")
POWER_OFF_VALUE = "PowerOff"


def _looks_like_air_purifier(appliance: dict, capabilities: dict) -> bool:
    """Heuristic: should we expose this appliance as a fan entity?"""
    appliance_type = (extract_appliance_type(appliance) or "").upper()
    model = (extract_appliance_model(appliance) or "").upper()
    if "AIRPURIFIER" in appliance_type or "PURIFIER" in appliance_type:
        return True
    if "PURE" in model or "AIR" in model:
        return True
    # Capability fingerprint covers any future appliance that exposes both
    # the work-mode enum and a fanspeed integer.
    if isinstance(capabilities, dict) and "Workmode" in capabilities and "Fanspeed" in capabilities:
        return True
    return False


def _derive_speed_range(capabilities: dict) -> tuple[int, int]:
    fs = (capabilities or {}).get("Fanspeed") or {}
    lo, hi = fs.get("min"), fs.get("max")
    if isinstance(lo, int) and isinstance(hi, int) and lo < hi:
        return (lo, hi)
    values = fs.get("values")
    if isinstance(values, dict) and values:
        ints: list[int] = []
        for k in values.keys():
            try:
                ints.append(int(k))
            except (TypeError, ValueError):
                continue
        if len(ints) >= 2:
            return (min(ints), max(ints))
    return DEFAULT_SPEED_RANGE


def _derive_preset_modes(capabilities: dict) -> list[str]:
    wm = (capabilities or {}).get("Workmode") or {}
    values = wm.get("values")
    if isinstance(values, dict) and values:
        modes = [m for m in values.keys() if m and m != POWER_OFF_VALUE]
        if modes:
            return modes
    return list(DEFAULT_PRESET_MODES)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Electrolux fans based on a config entry."""
    coordinator = entry.runtime_data.coordinator
    seen_entities: set[str] = set()

    @callback
    def async_add_new_entities() -> None:
        new_entities: list[FanEntity] = []
        appliances = coordinator.data.appliances if coordinator.data else []
        for appliance in appliances:
            appliance_id = extract_appliance_id(appliance)
            if not appliance_id:
                continue

            capabilities = coordinator.get_capabilities(appliance_id) or {}
            if not _looks_like_air_purifier(appliance, capabilities):
                continue

            uid = f"{entry.entry_id}_{appliance_id}_fan"
            if uid not in seen_entities:
                seen_entities.add(uid)
                new_entities.append(ElectroluxAirPurifier(coordinator, appliance_id))

        if new_entities:
            async_add_entities(new_entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class ElectroluxAirPurifier(ElectroluxBaseEntity, FanEntity):
    """Air purifier entity, capabilities-aware with PURE A9 defaults."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_translation_key = "air_purifier"

    def __init__(self, coordinator, appliance_id: str) -> None:
        """Initialize the fan."""
        super().__init__(coordinator, appliance_id)
        self._attr_unique_id = f"{appliance_id}_fan"
        self.entity_id = f"fan.{DOMAIN}_{appliance_id}"
        capabilities = coordinator.get_capabilities(appliance_id) or {}
        self._speed_range = _derive_speed_range(capabilities)
        # HA requires translation keys to match `[a-z0-9-_]+`, so the preset
        # modes we expose are lowercased. The cloud Workmode payload keeps
        # its original PascalCase, so preserve a reverse map for sending.
        original_modes = _derive_preset_modes(capabilities)
        self._preset_mode_to_workmode: dict[str, str] = {m.lower(): m for m in original_modes}
        self._attr_preset_modes = list(self._preset_mode_to_workmode.keys())

    @property
    def is_on(self) -> bool | None:
        appliance = self._appliance
        if not appliance:
            return None
        work_mode = appliance.get("properties", {}).get("reported", {}).get("Workmode")
        if work_mode is None:
            return None
        return work_mode != POWER_OFF_VALUE

    @property
    def preset_mode(self) -> str | None:
        appliance = self._appliance
        if not appliance:
            return None
        work_mode = appliance.get("properties", {}).get("reported", {}).get("Workmode")
        if work_mode is None:
            return None
        # Cloud reports Workmode in its original casing; expose lowercase to HA.
        lowered = str(work_mode).lower()
        if lowered in self._preset_mode_to_workmode:
            return lowered
        return None

    @property
    def percentage(self) -> int | None:
        appliance = self._appliance
        if not appliance:
            return None
        speed = appliance.get("properties", {}).get("reported", {}).get("Fanspeed")
        if speed is None:
            return None
        return ranged_value_to_percentage(self._speed_range, speed)

    @property
    def speed_count(self) -> int:
        return int_states_in_range(self._speed_range)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        payload: dict[str, Any] = {}
        if preset_mode:
            payload["Workmode"] = self._preset_mode_to_workmode.get(preset_mode, preset_mode)
        elif self.preset_mode is None:
            # Restore something useful: prefer "auto" if the appliance offers
            # it, otherwise the first listed preset mode.
            modes = self._attr_preset_modes or []
            chosen = "auto" if "auto" in modes else (modes[0] if modes else "auto")
            payload["Workmode"] = self._preset_mode_to_workmode.get(chosen, "Auto")

        if percentage:
            payload["Fanspeed"] = math.ceil(
                percentage_to_ranged_value(self._speed_range, percentage)
            )
            payload["Workmode"] = self._preset_mode_to_workmode.get("manual", "Manual")

        await self._send_commands(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_commands({"Workmode": POWER_OFF_VALUE})

    async def async_set_percentage(self, percentage: int) -> None:
        speed = math.ceil(percentage_to_ranged_value(self._speed_range, percentage))
        manual_mode = self._preset_mode_to_workmode.get("manual", "Manual")
        await self._send_commands({"Workmode": manual_mode, "Fanspeed": speed})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        # Map HA's lowercase preset back to the cloud's original casing.
        workmode = self._preset_mode_to_workmode.get(preset_mode, preset_mode)
        await self._send_commands({"Workmode": workmode})

    async def _send_commands(self, payload: dict[str, Any]) -> None:
        client = self.coordinator.client
        await client.async_send_command(self._appliance_id, payload)
        await self.coordinator.async_request_refresh()
