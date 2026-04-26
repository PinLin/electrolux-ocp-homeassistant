"""Electrolux custom integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ElectroluxApiClient, ElectroluxApiError, ElectroluxAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    CONF_WS_BASE_URL,
    DEFAULT_API_BASE_URL,
)
from .coordinator import ElectroluxDataUpdateCoordinator
from .models import ElectroluxConfigEntry, ElectroluxRuntimeData

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.FAN,
]


async def async_setup_entry(hass: HomeAssistant, entry: ElectroluxConfigEntry) -> bool:
    """Set up Electrolux from a config entry."""
    client = ElectroluxApiClient(
        session=async_get_clientsession(hass),
        api_base_url=entry.data.get(CONF_API_BASE_URL, DEFAULT_API_BASE_URL),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        api_key=entry.data.get(CONF_API_KEY),
        country_code=entry.data.get(CONF_COUNTRY_CODE),
        email=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        ws_base_url=entry.data.get(CONF_WS_BASE_URL),
    )

    # Simple strategy: if we don't have a token, log in.
    if not client.access_token:
        if not entry.data.get(CONF_PASSWORD):
            raise ConfigEntryAuthFailed("Authentication token missing and no password stored.")

        try:
            await client.async_login()
        except ElectroluxAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except ElectroluxApiError as err:
            raise ConfigEntryNotReady(f"API error: {err}") from err

    coordinator = ElectroluxDataUpdateCoordinator(hass, client=client, entry=entry)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ElectroluxAuthError as err:
        try:
            # Token might be expired, try to login again
            await client.async_login()
            await coordinator.async_config_entry_first_refresh()
        except ElectroluxAuthError as inner_err:
            raise ConfigEntryAuthFailed(str(inner_err)) from inner_err
    except ElectroluxApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = ElectroluxRuntimeData(client=client, coordinator=coordinator)
    # Cleanly cancel the WebSocket task when the entry is unloaded/reloaded.
    entry.async_on_unload(coordinator.async_stop_websocket)
    # Start the WebSocket task once: its internal retry loop self-heals.
    # Decoupled from polling cadence so the polling tick can't delay WS recovery.
    coordinator.start_websocket()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectroluxConfigEntry) -> bool:
    """Unload an Electrolux config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
