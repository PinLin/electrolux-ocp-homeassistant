"""Electrolux custom integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .api import ElectroluxApiClient, extract_appliance_id
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    CONF_WS_BASE_URL,
    DEFAULT_API_BASE_URL,
    DOMAIN,
)
from .coordinator import ElectroluxDataUpdateCoordinator
from .models import ElectroluxConfigEntry, ElectroluxRuntimeData

_LOGGER = logging.getLogger(__name__)

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

    # Simple strategy: if we don't have a token, we can't proceed. The config
    # flow always discards the password after a successful login/reauth (it
    # persists rotated tokens instead), so there is never a password on the
    # entry to fall back to here — the only path forward is reconfiguring.
    if not client.access_token:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_token_missing",
        )

    coordinator = ElectroluxDataUpdateCoordinator(hass, client=client, entry=entry)

    # The coordinator already translates ElectroluxAuthError into
    # ConfigEntryAuthFailed and other ElectroluxApiError into UpdateFailed
    # (which async_config_entry_first_refresh turns into ConfigEntryNotReady)
    # before this call returns, so those two HA exceptions are the only
    # failure modes here — let them propagate to trigger reauth / HA's
    # built-in retry-with-backoff respectively.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = ElectroluxRuntimeData(client=client, coordinator=coordinator)
    # Cleanly cancel the WebSocket task when the entry is unloaded/reloaded.
    entry.async_on_unload(coordinator.async_stop_websocket)
    # Start the WebSocket task once: its internal retry loop self-heals.
    # Decoupled from polling cadence so the polling tick can't delay WS recovery.
    coordinator.start_websocket()

    # Stale-device cleanup now runs inside coordinator._async_update_data on
    # every successful poll, so first-refresh already covered it here.

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectroluxConfigEntry) -> bool:
    """Unload an Electrolux config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ElectroluxConfigEntry,
    device: DeviceEntry,
) -> bool:
    """Allow user to remove a device from the HA UI.

    Conservative policy: only allow removal when the appliance is no
    longer present in coordinator data. That way an accidental click
    on an active appliance can't tear down its entities — the user
    has to remove it from their Electrolux account first (or the
    cloud has stopped reporting it), which is the case where leftover
    HA state is actually noise.
    """
    coordinator = entry.runtime_data.coordinator
    if coordinator.data is None:
        # Without fresh data we can't tell — refuse rather than guess.
        return False

    known_ids = {
        extract_appliance_id(a)
        for a in coordinator.data.appliances
        if extract_appliance_id(a)
    }
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier in known_ids:
            return False
    return True
