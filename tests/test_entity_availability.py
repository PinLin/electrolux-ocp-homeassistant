"""Tests pinning entity availability against coordinator update failures.

Background: ElectroluxBaseEntity overrides _handle_coordinator_update to
suppress async_write_ha_state unless the snapshot of _state_attrs (value
properties only, e.g. is_on) changed. That snapshot never includes
availability, so a coordinator refresh failure (last_update_success flips
to False while coordinator.data is untouched) never gets broadcast — the
entity stays stuck on its last good value in HA's state machine instead of
going unavailable. These tests pin the fix: availability must be part of
what triggers a broadcast, independent of whether the tracked value moved.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.api import ElectroluxAuthError
from custom_components.electrolux_ocp.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.electrolux_ocp.coordinator import (
    ElectroluxDataUpdateCoordinator,
)
from custom_components.electrolux_ocp.models import ElectroluxData
from custom_components.electrolux_ocp.switch import ElectroluxSwitch


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Suppress PHCC's lingering-thread assertion for the full-stack test
    below. The HA-managed aiohttp client session created during a real
    config entry setup spawns a resolver thread we can't deterministically
    join in test scope (see tests/test_init.py for the same pattern)."""
    yield


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
    return {"applianceId": aid, "properties": {"reported": dict(reported)}}


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
    coord.data = ElectroluxData(appliances=list(appliances), user=None, capabilities={})


def _make_switch(coord: ElectroluxDataUpdateCoordinator) -> ElectroluxSwitch:
    coord._capabilities_cache["appl-1"] = {
        "UILight": {"access": "readwrite", "type": "boolean"}
    }
    _seed(coord, _appliance(UILight=False))
    switch = ElectroluxSwitch(coord, "appl-1", "UILight")
    # Mirrors what async_added_to_hass does (minus the base-class listener
    # registration, which isn't needed for exercising _handle_coordinator_update
    # directly): seed the baseline snapshot so the first *real* update is
    # judged against a known prior state.
    switch._refresh_last_broadcast()
    switch.async_write_ha_state = MagicMock()
    return switch


async def test_broadcasts_when_update_fails_without_data_change(
    hass: HomeAssistant,
) -> None:
    """A coordinator refresh failure must broadcast even though the tracked
    value (is_on) hasn't moved, so HA can flip the entity to unavailable."""
    coord = _make_coordinator(hass)
    switch = _make_switch(coord)

    # Simulate ConfigEntryAuthFailed/UpdateFailed: last_update_success flips,
    # coordinator.data is left untouched (this is exactly what the base
    # DataUpdateCoordinator does on a failed refresh).
    coord.last_update_success = False

    switch._handle_coordinator_update()

    switch.async_write_ha_state.assert_called_once()
    assert switch.available is False


async def test_broadcasts_when_update_recovers_without_data_change(
    hass: HomeAssistant,
) -> None:
    """Recovery (False -> True) must also broadcast so a stale 'unavailable'
    gets cleared even if the underlying value didn't change either."""
    coord = _make_coordinator(hass)
    switch = _make_switch(coord)

    coord.last_update_success = False
    switch._handle_coordinator_update()
    switch.async_write_ha_state.reset_mock()

    coord.last_update_success = True
    switch._handle_coordinator_update()

    switch.async_write_ha_state.assert_called_once()
    assert switch.available is True


async def test_no_broadcast_when_value_and_availability_unchanged(
    hass: HomeAssistant,
) -> None:
    """The fan-out suppression optimization must survive: a coordinator
    refresh that changes neither the tracked value nor availability should
    not trigger a redundant async_write_ha_state call."""
    coord = _make_coordinator(hass)
    switch = _make_switch(coord)

    switch._handle_coordinator_update()

    switch.async_write_ha_state.assert_not_called()


def _make_full_client(appliances: list[dict[str, Any]]) -> AsyncMock:
    """A client double for a *real* config entry setup (hass.config_entries
    .async_setup), as opposed to _make_client() above which only backs a
    directly-instantiated coordinator."""
    client = AsyncMock()
    client.access_token = "tok"
    client.refresh_token = "ref"
    client.api_key = "key"
    client.api_base_url = "https://api.example/"
    client.ws_base_url = "wss://ws.example/"
    client.access_token_expires_at = None
    client.async_login = AsyncMock(return_value=None)
    client.async_ensure_valid_token = AsyncMock(return_value=False)
    client.async_ensure_regional_config = AsyncMock(return_value=False)
    client.async_get_appliances = AsyncMock(return_value=appliances)
    client.async_get_current_user = AsyncMock(
        return_value={"email": "user@example.com", "userName": "sample"}
    )
    # None here (rather than an explicit dict) intentionally exercises the
    # StaticPureA9Provider capability fallback for the PUREA9 fixture used
    # below, matching how a real un-provisioned/degraded OCP response looks.
    client.async_get_capabilities = AsyncMock(return_value=None)
    client.set_on_token_update = MagicMock()
    client.ws_connect = MagicMock()
    return client


async def test_end_to_end_auth_failure_makes_entity_unavailable_and_recovers(
    hass: HomeAssistant, sample_appliance_purea9: dict[str, Any]
) -> None:
    """Exercises the real coordinator -> HA's DataUpdateCoordinator ->
    listener-dispatch path end to end, unlike the unit tests above which
    call switch._handle_coordinator_update() directly.

    The other half of the production bug this pins down: whether HA's own
    `_async_refresh()` actually calls `async_update_listeners()` after a
    `ConfigEntryAuthFailed`. It currently does -- the `auth_failed` guard
    sits only a few lines above the dispatch call in
    homeassistant/helpers/update_coordinator.py -- but that is an assumption
    about HA core's internals that a future HA version could invalidate
    while every other test in this suite (which drives
    _handle_coordinator_update directly) stays green.
    """
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
    fake_client = _make_full_client([sample_appliance_purea9])
    appliance_id = sample_appliance_purea9["applianceId"]
    entity_id = f"switch.{DOMAIN}_{appliance_id}_uilight"

    with patch(
        "custom_components.electrolux_ocp.ElectroluxApiClient",
        return_value=fake_client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"  # fixture reports UILight: True

        # Simulate the server rejecting the token outright: _do_update_data
        # raises ConfigEntryAuthFailed, which the coordinator's own
        # _async_update_data does NOT catch (only UpdateFailed is), so it
        # propagates into HA's real _async_refresh().
        fake_client.async_ensure_valid_token = AsyncMock(
            side_effect=ElectroluxAuthError("401: token rejected")
        )
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unavailable"

        # Recovery: token becomes valid again on the next refresh.
        fake_client.async_ensure_valid_token = AsyncMock(return_value=False)
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
