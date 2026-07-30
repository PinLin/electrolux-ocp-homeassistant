"""Exponential + jittered backoff for the WebSocket reconnect loop.

Covers:
* the backoff formula itself (normal exponential path, rate-limited floor)
* the reconnect loop actually wiring failures into the counter and resetting
  it on a successful connect
* the handshake-error branches routing through the same formula
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from aiohttp import WSServerHandshakeError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrolux_ocp.api import (
    ElectroluxAuthError,
    ElectroluxRateLimitError,
)
from custom_components.electrolux_ocp.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.electrolux_ocp.coordinator import (
    ElectroluxDataUpdateCoordinator,
    WS_RATE_LIMIT_BACKOFF_SECONDS,
    WS_TOKEN_FRESH_SECONDS,
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


class _ConnectThenFailMidStream:
    """Fake ws_connect() result: enters cleanly, then blows up mid-read."""

    def __call__(self, appliance_ids):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RuntimeError("mid-stream boom")


# --------------------------------------------------------------------------- #
# Formula unit tests
# --------------------------------------------------------------------------- #
async def test_backoff_formula_normal_path_exponential_and_capped(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.5,
    ):
        coord._ws_consecutive_failures = 0
        assert coord._ws_backoff_seconds() == pytest.approx(30 * 0.5)
        coord._ws_consecutive_failures = 1
        assert coord._ws_backoff_seconds() == pytest.approx(60 * 0.5)
        coord._ws_consecutive_failures = 3
        assert coord._ws_backoff_seconds() == pytest.approx(240 * 0.5)
        coord._ws_consecutive_failures = 10  # far past the cap
        assert coord._ws_backoff_seconds() == pytest.approx(300 * 0.5)


async def test_backoff_formula_rate_limited_never_below_floor(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.0,
    ):
        assert coord._ws_backoff_seconds(rate_limited=True) == pytest.approx(300.0)
    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.5,
    ):
        assert coord._ws_backoff_seconds(rate_limited=True) == pytest.approx(450.0)


# --------------------------------------------------------------------------- #
# Reconnect-loop wiring: consecutive failures grow, reset on success.
# --------------------------------------------------------------------------- #
async def test_ws_loop_backoff_grows_and_resets_after_successful_connect(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord.data = ElectroluxData(
        appliances=[{"applianceId": "A1"}], user=None, capabilities={}
    )
    coord._ws_active = True

    behaviors = ["fail", "fail", "connect_then_fail", "fail"]
    call_index = 0

    def side_effect(appliance_ids):
        nonlocal call_index
        call_index += 1
        if call_index >= len(behaviors):
            coord._ws_active = False
        behavior = behaviors[call_index - 1]
        if behavior == "fail":
            raise RuntimeError(f"boom-{call_index}")
        return _ConnectThenFailMidStream()

    coord._client.ws_connect = MagicMock(side_effect=side_effect)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch(
        "custom_components.electrolux_ocp.coordinator.asyncio.sleep",
        new=AsyncMock(side_effect=fake_sleep),
    ), patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=1.0,
    ):
        await coord._async_websocket_loop()

    assert call_index == 4
    # 30 (1st failure), 60 (2nd), 30 (reset by the successful connect, then
    # fails again), 60 (still counting from the post-reset failure).
    assert sleeps == [30.0, 60.0, 30.0, 60.0]


# --------------------------------------------------------------------------- #
# Handshake-error branches route through the same formula.
# --------------------------------------------------------------------------- #
async def test_handshake_error_non_auth_status_uses_formula_and_increments(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    assert coord._ws_consecutive_failures == 0
    err = WSServerHandshakeError(MagicMock(), (), status=500, message="oops")
    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.5,
    ):
        backoff = await coord._handle_ws_handshake_error(err, False)
    assert backoff == pytest.approx(30 * 0.5)
    assert coord._ws_consecutive_failures == 1


async def test_ws_auth_death_exits_loop_without_sleeping(
    hass: HomeAssistant,
) -> None:
    # When the handshake handler declares the WS dead (refresh rejected →
    # reauth pending), the loop must exit immediately. Sleeping a backoff
    # first would leave the task alive for up to 300 s, blocking the
    # poll-driven restart which requires task.done().
    coord = _make_coordinator(hass)
    coord.data = ElectroluxData(
        appliances=[{"applianceId": "A1"}], user=None, capabilities={}
    )
    coord._ws_active = True
    coord._client.access_token_expires_at = None
    coord._client.async_refresh_token = AsyncMock(
        side_effect=ElectroluxAuthError("invalid_grant")
    )
    coord._client.ws_connect = MagicMock(
        side_effect=WSServerHandshakeError(MagicMock(), (), status=401, message="denied")
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch(
        "custom_components.electrolux_ocp.coordinator.asyncio.sleep",
        new=AsyncMock(side_effect=fake_sleep),
    ), patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=1.0,
    ):
        await coord._async_websocket_loop()

    assert sleeps == []
    assert coord._ws_active is False
    coord._client.ws_connect.assert_called_once()


async def test_handshake_error_fresh_token_skips_refresh_and_backs_off(
    hass: HomeAssistant,
) -> None:
    """A 401 while the access token still has plenty of life left must not
    trigger a refresh -- refreshing here would be both pointless and risks
    hitting OCP's refresh rate limit (cas_3404).

    This branch was unreachable before the JWT-`exp` fallback existed: every
    fixture pinned `access_token_expires_at` to None (see the other tests in
    this module), which is exactly what a freshly restarted client used to
    look like -- no test ever exercised the "fresh token" side of this
    condition. Now that the fallback seeds a real value on construction and
    after every token apply, this branch is live in production and needs
    its own coverage.
    """
    coord = _make_coordinator(hass)
    coord._client.access_token_expires_at = time.time() + WS_TOKEN_FRESH_SECONDS + 100
    coord._client.async_refresh_token = AsyncMock()
    err = WSServerHandshakeError(MagicMock(), (), status=401, message="denied")

    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.5,
    ):
        backoff = await coord._handle_ws_handshake_error(err, False)

    assert backoff == pytest.approx(30 * 0.5)
    coord._client.async_refresh_token.assert_not_called()
    assert coord._ws_consecutive_failures == 1


async def test_handshake_error_rate_limited_branch_uses_floor_and_increments(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    coord._client.access_token_expires_at = None
    coord._client.async_refresh_token = AsyncMock(
        side_effect=ElectroluxRateLimitError("cas_3404")
    )
    err = WSServerHandshakeError(MagicMock(), (), status=401, message="denied")
    with patch(
        "custom_components.electrolux_ocp.coordinator.random.uniform",
        return_value=0.0,
    ):
        backoff = await coord._handle_ws_handshake_error(err, False)
    assert backoff >= WS_RATE_LIMIT_BACKOFF_SECONDS
    assert backoff == pytest.approx(300.0)
    assert coord._ws_consecutive_failures == 1
