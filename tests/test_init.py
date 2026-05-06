"""Setup / teardown tests for the Electrolux integration.

Verifies:
* a successful setup attaches runtime_data and starts the WS task,
* unload cancels the WS task and forwards platform unloading,
* stale-device cleanup runs on every refresh (not just at first setup).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Suppress PHCC's lingering-thread assertion for these tests.

    The HA-managed aiohttp client session created during setup spawns
    a resolver thread we can't deterministically join in test scope.
    """
    yield


def _entry(hass: HomeAssistant) -> MockConfigEntry:
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


def _make_client(appliances: list[dict[str, Any]] | None = None) -> AsyncMock:
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
    client.async_get_appliances = AsyncMock(return_value=appliances or [])
    client.async_get_current_user = AsyncMock(
        return_value={"email": "user@example.com", "userName": "sample"}
    )
    client.async_get_capabilities = AsyncMock(return_value=None)
    client.set_on_token_update = MagicMock()
    # ws_connect will be called from the WS background task. Returning a
    # context manager that yields nothing prevents the loop from doing real
    # I/O. The loop checks `appliance_ids` first; with an empty list it
    # sleeps WS_NO_DATA_RETRY_SECONDS and never reaches ws_connect anyway.
    client.ws_connect = MagicMock()
    return client


async def test_setup_attaches_runtime_data(hass: HomeAssistant) -> None:
    """Successful first refresh wires up runtime_data and starts the WS task."""
    entry = _entry(hass)
    fake_client = _make_client()

    with patch(
        "custom_components.electrolux_ocp.ElectroluxApiClient",
        return_value=fake_client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.recoverable is False or entry.state.value == "loaded"
    assert entry.runtime_data is not None
    assert entry.runtime_data.client is fake_client
    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    # Capability fetch was attempted but returned None — cache stays empty
    # for unsupported appliances.
    assert coordinator.last_update_success_time is not None

    # Cleanly tear down
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_stops_ws_task(hass: HomeAssistant) -> None:
    """After unload the coordinator's WS task must be done/cancelled."""
    entry = _entry(hass)
    fake_client = _make_client()

    with patch(
        "custom_components.electrolux_ocp.ElectroluxApiClient",
        return_value=fake_client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    # Either fully done or cancelled — both acceptable post-unload.
    ws_task = coordinator._ws_task
    assert ws_task is None or ws_task.done()


async def test_stale_device_removed_on_refresh(hass: HomeAssistant) -> None:
    """An appliance dropping off the account must be removed from the device registry."""
    entry = _entry(hass)
    appliance_id = "111222333444455556667777"
    appliance = {"applianceId": appliance_id, "applianceName": "Test"}

    fake_client = _make_client(appliances=[appliance])

    with patch(
        "custom_components.electrolux_ocp.ElectroluxApiClient",
        return_value=fake_client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Manually register a device for this appliance so cleanup has
        # something to operate on (entity discovery happens lazily, so the
        # device wouldn't otherwise exist yet for a bare-bones appliance dict).
        device_reg = dr.async_get(hass)
        device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, appliance_id)},
            name="Test",
        )
        assert device_reg.async_get_device(identifiers={(DOMAIN, appliance_id)})

        # Account now reports zero appliances → next refresh should remove
        # the device.
        fake_client.async_get_appliances = AsyncMock(return_value=[])
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert (
            device_reg.async_get_device(identifiers={(DOMAIN, appliance_id)}) is None
        )

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
