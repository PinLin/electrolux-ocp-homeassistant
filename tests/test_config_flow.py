"""Tests for the Electrolux config flow.

Covers the happy path, the two error branches (invalid_auth /
cannot_connect), the unique-id abort, and the reauth flow. The
``ElectroluxApiClient`` is patched so the flow never makes a real network
call — the goal is to verify branching logic, not the API client.

``async_setup_entry`` is also patched out: post-create entry setup spins
up the coordinator + a WS background task, which would be irrelevant noise
for config-flow assertions and would also leak threads via HA's shared
aiohttp session under PHCC's lingering-thread check.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.const import (
    CONF_COUNTRY_CODE,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    """Skip the full coordinator/WS setup; we only test the flow itself."""
    with patch(
        "custom_components.electrolux_ocp.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Override PHCC's verify_cleanup for this file.

    A successful CREATE_ENTRY result causes HA to spin up entity/device
    registries and an aiohttp resolver thread under the hood. Those are
    framework-owned (not from our code), but PHCC's lingering-thread
    assertion still catches them. Suppressing the check keeps these flow
    tests focused on behaviour we control.
    """
    yield


def _make_client(
    *,
    login: AsyncMock | None = None,
    appliances: list[dict[str, Any]] | None = None,
    user: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build a fake ElectroluxApiClient instance with sensible defaults."""
    client = AsyncMock()
    client.async_login = login or AsyncMock(return_value=None)
    client.async_get_appliances = AsyncMock(return_value=appliances or [])
    client.async_get_current_user = AsyncMock(
        return_value=user or {"email": "user@example.com", "userName": "sample"}
    )
    client.access_token = "access-token-1"
    client.refresh_token = "refresh-token-1"
    client.api_key = "api-key-1"
    client.api_base_url = "https://api.example/"
    client.ws_base_url = "wss://ws.example/"
    return client


@pytest.fixture
def fake_client() -> AsyncMock:
    return _make_client()


@pytest.mark.asyncio
async def test_user_flow_creates_entry(hass: HomeAssistant, fake_client: AsyncMock) -> None:
    """Happy path: valid creds → entry created with title=email."""
    with patch(
        "custom_components.electrolux_ocp.config_flow.ElectroluxApiClient",
        return_value=fake_client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "user@example.com",
                "password": "secret",
                CONF_COUNTRY_CODE: "TW",
            },
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "user@example.com"
    assert result2["data"]["access_token"] == "access-token-1"
    assert result2["data"]["refresh_token"] == "refresh-token-1"
    # Password must NOT be persisted (we keep the rotated tokens instead).
    assert "password" not in result2["data"]


@pytest.mark.asyncio
async def test_user_flow_invalid_auth(hass: HomeAssistant) -> None:
    """ElectroluxAuthError → form re-shown with invalid_auth error."""
    from custom_components.electrolux_ocp.api import ElectroluxAuthError

    failing = _make_client(login=AsyncMock(side_effect=ElectroluxAuthError("nope")))
    with patch(
        "custom_components.electrolux_ocp.config_flow.ElectroluxApiClient",
        return_value=failing,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "user@example.com",
                "password": "wrong",
                CONF_COUNTRY_CODE: "TW",
            },
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """ElectroluxApiError → cannot_connect."""
    from custom_components.electrolux_ocp.api import ElectroluxApiError

    failing = _make_client(login=AsyncMock(side_effect=ElectroluxApiError("network")))
    with patch(
        "custom_components.electrolux_ocp.config_flow.ElectroluxApiClient",
        return_value=failing,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "user@example.com",
                "password": "x",
                CONF_COUNTRY_CODE: "TW",
            },
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_user_flow_aborts_when_account_already_configured(
    hass: HomeAssistant, fake_client: AsyncMock
) -> None:
    """Same email → unique_id collision → abort already_configured."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={"username": "user@example.com"},
    ).add_to_hass(hass)

    with patch(
        "custom_components.electrolux_ocp.config_flow.ElectroluxApiClient",
        return_value=fake_client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": "user@example.com",
                "password": "secret",
                CONF_COUNTRY_CODE: "TW",
            },
        )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_reauth_updates_tokens(hass: HomeAssistant, fake_client: AsyncMock) -> None:
    """Reauth path swaps in fresh tokens without recreating the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            "username": "user@example.com",
            CONF_COUNTRY_CODE: "TW",
            "access_token": "old",
            "refresh_token": "old",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.electrolux_ocp.config_flow.ElectroluxApiClient",
        return_value=fake_client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "newsecret"}
        )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data["access_token"] == "access-token-1"
    assert entry.data["refresh_token"] == "refresh-token-1"
