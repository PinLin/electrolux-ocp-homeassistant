"""Optimistic write flow tests.

Background: OCP's command pipeline is eventually consistent — a PUT
/command is acknowledged by the cloud, then forwarded to the device, then
the device reports back via WS. The previous code called
``async_request_refresh()`` immediately after PUT, so the REST snapshot
returned the *old* reported value and the UI flickered ON → OFF → ON.

These tests pin the behaviour of the optimistic overlay: a paint-locally
mechanism that holds the user's chosen value in coordinator data until
WS/REST confirms it, the TTL expires, or an API error clears it.
"""

from __future__ import annotations

import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.electrolux_ocp.coordinator import (
    OPTIMISTIC_TTL_SECONDS,
    ElectroluxDataUpdateCoordinator,
)
from custom_components.electrolux_ocp.models import ElectroluxData


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_COUNTRY_CODE: "TW",
            "access_token": "tok",
            CONF_REFRESH_TOKEN: "ref",
            CONF_API_KEY: "key",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _make_client() -> AsyncMock:
    client = AsyncMock()
    client.access_token = "tok"
    client.refresh_token = "ref"
    client.api_key = "key"
    client.api_base_url = "https://api.example/"
    client.ws_base_url = "wss://ws.example/"
    client.access_token_expires_at = None
    client.set_on_token_update = MagicMock()
    return client


def _appliance(aid: str = "appl-1", **reported: Any) -> dict[str, Any]:
    base = {
        "applianceId": aid,
        "properties": {"reported": dict(reported)},
    }
    return base


def _make_coordinator(hass: HomeAssistant) -> ElectroluxDataUpdateCoordinator:
    entry = _make_entry(hass)
    client = _make_client()
    capabilities_provider = SimpleNamespace(async_fetch=AsyncMock(return_value={}))
    return ElectroluxDataUpdateCoordinator(
        hass,
        client=client,
        entry=entry,
        capabilities_provider=capabilities_provider,  # type: ignore[arg-type]
    )


def _seed(coord: ElectroluxDataUpdateCoordinator, *appliances: dict[str, Any]) -> None:
    coord.data = ElectroluxData(
        appliances=list(appliances), user=None, capabilities={}
    )


def _reported(coord: ElectroluxDataUpdateCoordinator, aid: str) -> dict[str, Any]:
    appliance = coord.get_appliance(aid)
    assert appliance is not None
    return appliance.get("properties", {}).get("reported", {})


async def test_optimistic_update_paints_value_immediately(hass: HomeAssistant) -> None:
    """The desired value shows up in coordinator data without waiting for WS/REST."""
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))

    coord.apply_optimistic_update("appl-1", {"UILight": True})

    assert _reported(coord, "appl-1")["UILight"] is True


async def test_stale_rest_poll_does_not_overwrite_pending(
    hass: HomeAssistant,
) -> None:
    """REST coming back with the pre-command value must not undo the overlay.

    This is the exact race that produced the "flicker back to off" symptom
    — the cloud's reported snapshot was stale relative to what the user
    just commanded.
    """
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    coord.apply_optimistic_update("appl-1", {"UILight": True})

    # Simulate a REST poll returning before the cloud caught up: stale data.
    stale = [_appliance(UILight=False)]
    overlaid = coord._merge_pending_into_appliances(stale)

    assert overlaid[0]["properties"]["reported"]["UILight"] is True


async def test_ws_confirmation_clears_pending(hass: HomeAssistant) -> None:
    """A WS push whose value matches the pending write clears the entry.

    After clearing, a subsequent stale REST snapshot is *not* protected —
    that's the right behaviour: the cloud has confirmed once already, so
    the next disagreement should reach the UI rather than be silently
    masked.
    """
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    coord.apply_optimistic_update("appl-1", {"UILight": True})

    # WS push with the confirmed value flows through the same merge helper.
    confirmed = [_appliance(UILight=True)]
    coord._merge_pending_into_appliances(confirmed)

    assert "appl-1" not in coord._optimistic_pending


async def test_pending_expires_after_ttl(hass: HomeAssistant) -> None:
    """Past the TTL the overlay drops and reality wins, even if reality
    disagrees with the user's last action.

    This is the "device silently rejected my command" honesty case.
    """
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    coord.apply_optimistic_update("appl-1", {"UILight": True})

    # Force the pending entry past its TTL without sleeping.
    pending = coord._optimistic_pending["appl-1"]
    value, original, _ = pending["UILight"]
    pending["UILight"] = (value, original, time.monotonic() - 1.0)

    overlaid = coord._merge_pending_into_appliances([_appliance(UILight=False)])
    assert overlaid[0]["properties"]["reported"]["UILight"] is False
    assert "appl-1" not in coord._optimistic_pending


async def test_clear_optimistic_update_drops_pending(hass: HomeAssistant) -> None:
    """``clear_optimistic_update`` reverts to whatever ``reported`` says."""
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    coord.apply_optimistic_update("appl-1", {"UILight": True})
    assert _reported(coord, "appl-1")["UILight"] is True

    coord.clear_optimistic_update("appl-1", ["UILight"])

    assert _reported(coord, "appl-1")["UILight"] is False
    assert "appl-1" not in coord._optimistic_pending


async def test_clear_optimistic_update_no_pending_is_noop(
    hass: HomeAssistant,
) -> None:
    """Clearing a non-existent pending entry must not raise or broadcast."""
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    # Should not raise — represents the success-path cleanup pattern that
    # entity layers don't bother gating.
    coord.clear_optimistic_update("appl-1", ["UILight"])
    coord.clear_optimistic_update("unknown-appliance", ["UILight"])


async def test_apply_optimistic_update_preserves_other_reported_fields(
    hass: HomeAssistant,
) -> None:
    """Only the keys in the payload should change; everything else stays."""
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False, Ionizer=True, Fanspeed=3))

    coord.apply_optimistic_update("appl-1", {"UILight": True})

    reported = _reported(coord, "appl-1")
    assert reported == {"UILight": True, "Ionizer": True, "Fanspeed": 3}


async def test_overlay_does_not_mutate_source_list(hass: HomeAssistant) -> None:
    """The merge helper must not mutate the appliance dict it was given.

    The WS handler reuses ``appliance`` objects from coordinator data
    when nothing changed for that appliance; a hidden in-place mutation
    here would silently flip values across snapshots.
    """
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    coord.apply_optimistic_update("appl-1", {"UILight": True})

    incoming = [_appliance(UILight=False)]
    before = deepcopy(incoming)
    coord._merge_pending_into_appliances(incoming)
    assert incoming == before


async def test_no_pending_means_no_overlay_work(hass: HomeAssistant) -> None:
    """With no pending writes the helper returns the input untouched."""
    coord = _make_coordinator(hass)
    _seed(coord, _appliance(UILight=False))
    appliances = [_appliance(UILight=False)]
    assert coord._merge_pending_into_appliances(appliances) is appliances


@pytest.mark.parametrize(
    "ttl_constant_is_reasonable",
    [OPTIMISTIC_TTL_SECONDS >= 5 and OPTIMISTIC_TTL_SECONDS <= 30],
)
def test_ttl_constant_is_in_a_sensible_band(ttl_constant_is_reasonable: bool) -> None:
    """Guard against an accidental TTL change to something silly (0 or 600s).

    5s lower bound: anything shorter and a slow OCP forward (cloud → WS)
    will routinely expire the overlay. 30s upper bound: anything longer
    and a silently-rejected command would lie to the user for too long.
    """
    assert ttl_constant_is_reasonable


# ---------------------------------------------------------------------------
# Entity-layer wiring: switch sends optimistic *before* PUT and does NOT
# call ``async_request_refresh`` on the success path.
# ---------------------------------------------------------------------------

from custom_components.electrolux_ocp.switch import ElectroluxSwitch  # noqa: E402


def _switch_coordinator(hass: HomeAssistant) -> ElectroluxDataUpdateCoordinator:
    coord = _make_coordinator(hass)
    coord._capabilities_cache["appl-1"] = {
        "UILight": {"access": "readwrite", "type": "boolean"}
    }
    _seed(coord, _appliance(UILight=False))
    return coord


async def test_switch_turn_on_paints_optimistic_then_sends(
    hass: HomeAssistant,
) -> None:
    """Success path: optimistic apply happens, PUT runs, no immediate refresh."""
    coord = _switch_coordinator(hass)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.client.async_send_command = AsyncMock(return_value={})

    switch = ElectroluxSwitch(coord, "appl-1", "UILight")
    await switch.async_turn_on()

    coord.client.async_send_command.assert_awaited_once_with(
        "appl-1", {"UILight": True}
    )
    coord.async_request_refresh.assert_not_awaited()
    assert _reported(coord, "appl-1")["UILight"] is True


async def test_switch_turn_on_api_failure_clears_optimistic_and_refreshes(
    hass: HomeAssistant,
) -> None:
    """API failure path: drop optimistic state and force a true-up poll."""
    coord = _switch_coordinator(hass)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.client.async_send_command = AsyncMock(side_effect=RuntimeError("nope"))

    switch = ElectroluxSwitch(coord, "appl-1", "UILight")
    with pytest.raises(RuntimeError):
        await switch.async_turn_on()

    coord.async_request_refresh.assert_awaited_once()
    assert "appl-1" not in coord._optimistic_pending
    assert _reported(coord, "appl-1")["UILight"] is False
