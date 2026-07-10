"""Resilience fixes for the coordinator.

Covers:
* repair-issue clearing that survives a coordinator reload (problem 2)
* WebSocket self-heal after a fatal auth death, once REST recovers (problem 3)
* non-auth errors from proactive token refresh mapped to UpdateFailed (problem 4)
* a 401 immediately after a *successful* refresh not nuking into reauth (problem 5a)
* the "at most one WS refresh per connection cycle" guarantee actually holding (problem 6)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from aiohttp import WSServerHandshakeError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.api import (
    ElectroluxApiError,
    ElectroluxAuthError,
)
from custom_components.electrolux_ocp.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.electrolux_ocp.coordinator import (
    ISSUE_POLLING_FAILING,
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


def _issue_id(coord: ElectroluxDataUpdateCoordinator) -> str:
    return f"{ISSUE_POLLING_FAILING}_{coord.config_entry.entry_id}"


# --------------------------------------------------------------------------- #
# Problem 2: repair issue must clear even when the failure counter is fresh.
# --------------------------------------------------------------------------- #
async def test_polling_success_clears_stale_issue_from_previous_session(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    # Simulate an issue raised by a prior session; a reload zeroed the counter.
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(coord),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_POLLING_FAILING,
        translation_placeholders={"failures": "3"},
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(coord)) is not None
    assert coord._consecutive_failures == 0

    coord._note_polling_success()

    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(coord)) is None


# --------------------------------------------------------------------------- #
# Problem 3: a WS task that died on auth failure revives on a successful poll.
# --------------------------------------------------------------------------- #
async def test_dead_websocket_restarts_after_successful_poll(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord._client.async_ensure_valid_token = AsyncMock(return_value=False)
    coord._client.async_get_appliances = AsyncMock(return_value=[])
    coord._client.async_get_current_user = AsyncMock(return_value={})

    # A task that started and then died (WS auth failure path).
    dead = asyncio.create_task(asyncio.sleep(0))
    await dead
    coord._ws_task = dead
    coord._ws_active = False

    with patch.object(coord, "start_websocket") as mock_start:
        await coord._async_update_data()

    mock_start.assert_called_once()


async def test_stopped_websocket_not_revived_by_poll(hass: HomeAssistant) -> None:
    """A cleanly stopped WS (task cleared to None) must NOT be resurrected."""
    coord = _make_coordinator(hass)
    coord._client.async_ensure_valid_token = AsyncMock(return_value=False)
    coord._client.async_get_appliances = AsyncMock(return_value=[])
    coord._client.async_get_current_user = AsyncMock(return_value={})
    coord._ws_task = None
    coord._ws_active = False

    with patch.object(coord, "start_websocket") as mock_start:
        await coord._async_update_data()

    mock_start.assert_not_called()


# --------------------------------------------------------------------------- #
# Problem 4: a connection/5xx error from proactive refresh becomes UpdateFailed.
# --------------------------------------------------------------------------- #
async def test_proactive_refresh_connection_error_is_update_failed(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord._client.async_ensure_valid_token.side_effect = ElectroluxApiError(
        "500: server exploded"
    )

    with pytest.raises(UpdateFailed):
        await coord._do_update_data()


# --------------------------------------------------------------------------- #
# Problem 5a: 401 right after a *successful* refresh → UpdateFailed, not reauth.
# --------------------------------------------------------------------------- #
async def test_401_after_successful_refresh_does_not_reauth(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord._client.async_ensure_valid_token = AsyncMock(return_value=False)
    # Both the initial fetch and the post-refresh retry return 401.
    coord._client.async_get_appliances = AsyncMock(
        side_effect=ElectroluxAuthError("401: appliances")
    )
    # The refresh itself SUCCEEDS (server accepted the refresh token).
    coord._client.async_refresh_token = AsyncMock(return_value=None)

    with pytest.raises(UpdateFailed):
        await coord._do_update_data()
    # Must not have escalated to reauth.
    # (pytest.raises above already asserts it wasn't ConfigEntryAuthFailed,
    #  since that is not a subclass of UpdateFailed.)


# --------------------------------------------------------------------------- #
# Problem 6: at most one token refresh per WS connection cycle.
# --------------------------------------------------------------------------- #
async def test_ws_refreshes_at_most_once_per_connection_cycle(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord.data = ElectroluxData(
        appliances=[{"applianceId": "A1"}], user=None, capabilities={}
    )
    coord._ws_active = True
    coord._client.async_refresh_token = AsyncMock(return_value=None)

    call_count = 0

    def ws_connect_side_effect(appliance_ids):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # Let the loop terminate after this second handshake failure.
            coord._ws_active = False
        raise WSServerHandshakeError(
            MagicMock(), (), status=401, message="denied"
        )

    coord._client.ws_connect = MagicMock(side_effect=ws_connect_side_effect)

    with patch("asyncio.sleep", new=AsyncMock()):
        await coord._async_websocket_loop()

    assert call_count == 2, f"expected two handshake attempts, got {call_count}"
    assert coord._client.async_refresh_token.await_count == 1, (
        "WS must refresh at most once per connection cycle, "
        f"refreshed {coord._client.async_refresh_token.await_count} times"
    )
