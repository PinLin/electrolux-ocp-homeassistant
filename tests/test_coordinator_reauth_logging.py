"""Reauth-path logging tests.

Background: a field reauth on 2026-06-29 could not be diagnosed because the
coordinator raised ConfigEntryAuthFailed without logging the underlying
server response, and HA's log had rotated away by the time it was inspected.
These tests pin that both ConfigEntryAuthFailed sites in the coordinator
emit an ERROR record carrying the original failure reason, so the evidence
survives in the log even after the repair issue is the only visible trace.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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


async def test_reauth_after_failed_reactive_refresh_logs_reason(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coord = _make_coordinator(hass)
    # Initial appliances fetch 401s → reactive refresh path. Under the current
    # design only a refresh the server *rejects* (invalid_grant) escalates to
    # reauth; a 401 after a successful refresh is treated as transient. So make
    # the refresh itself fail here to exercise the reauth log.
    coord._client.async_get_appliances.side_effect = ElectroluxAuthError(
        "401: appliances rejected"
    )
    coord._client.async_refresh_token.side_effect = ElectroluxAuthError(
        "403: server said no"
    )

    with caplog.at_level(logging.ERROR, logger="custom_components.electrolux_ocp"):
        with pytest.raises(ConfigEntryAuthFailed):
            await coord._do_update_data()

    assert any(
        record.levelno == logging.ERROR and "403: server said no" in record.getMessage()
        for record in caplog.records
    )


async def test_reauth_from_ensure_valid_token_logs_reason(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coord = _make_coordinator(hass)
    coord._client.async_ensure_valid_token.side_effect = ElectroluxAuthError(
        "no refresh token available"
    )

    with caplog.at_level(logging.ERROR, logger="custom_components.electrolux_ocp"):
        with pytest.raises(ConfigEntryAuthFailed):
            await coord._do_update_data()

    assert any(
        record.levelno == logging.ERROR
        and "no refresh token available" in record.getMessage()
        for record in caplog.records
    )
