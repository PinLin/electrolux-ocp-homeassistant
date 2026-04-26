"""Config flow for Electrolux."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ElectroluxApiClient, ElectroluxApiError, ElectroluxAuthError
from .const import (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_COUNTRY_CODE,
    DEFAULT_API_BASE_URL,
    DOMAIN,
)


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COUNTRY_CODE, default="TW"): str,
    }
)


async def async_validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    client = ElectroluxApiClient(
        session=async_get_clientsession(hass),
        api_base_url=DEFAULT_API_BASE_URL,
        email=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        country_code=data.get(CONF_COUNTRY_CODE),
    )
    await client.async_login()

    appliances = await client.async_get_appliances()
    user = None
    try:
        user = await client.async_get_current_user()
    except ElectroluxApiError:
        pass

    email = None
    if isinstance(user, dict):
        email = user.get("email") or user.get("userName")
    if not email:
        email = data[CONF_USERNAME]

    if email:
        title = email
    elif appliances:
        title = f"{len(appliances)} appliances"
    else:
        title = "Electrolux"

    return {
        "title": title,
        "appliance_count": len(appliances),
        "unique_id": str(email),
        "access_token": client.access_token,
        "refresh_token": client.refresh_token,
        "api_key": client.api_key,
        "api_base_url": client.api_base_url,
        "ws_base_url": client.ws_base_url,
    }


class ElectroluxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Electrolux."""

    VERSION = 1

    _data: dict[str, Any]

    def __init__(self) -> None:
        """Initialize config flow."""
        super().__init__()
        self._data = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await async_validate_input(self.hass, user_input)
            except ElectroluxAuthError:
                errors["base"] = "invalid_auth"
            except ElectroluxApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                
                self._data = user_input.copy()
                self._data.pop(CONF_PASSWORD, None)
                
                self._data["title"] = info["title"]
                self._data["access_token"] = info["access_token"]
                self._data["refresh_token"] = info["refresh_token"]
                self._data["api_key"] = info["api_key"]
                self._data["api_base_url"] = info["api_base_url"]
                self._data["ws_base_url"] = info["ws_base_url"]

                return self.async_create_entry(title=self._data["title"], data=self._data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle re-authentication with Electrolux."""
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Confirm re-authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # We construct a data dict with the new password and existing username
            auth_data = {
                CONF_USERNAME: self._data.get(CONF_USERNAME),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_COUNTRY_CODE: self._data.get(CONF_COUNTRY_CODE, "TW"),
            }
            try:
                info = await async_validate_input(self.hass, auth_data)
            except ElectroluxAuthError:
                errors["base"] = "invalid_auth"
            except ElectroluxApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"
            else:
                self._data["access_token"] = info["access_token"]
                self._data["refresh_token"] = info["refresh_token"]
                self._data["api_key"] = info["api_key"]
                self._data["api_base_url"] = info["api_base_url"]
                self._data["ws_base_url"] = info["ws_base_url"]

                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data=self._data,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": self._data.get(CONF_USERNAME)},
        )
