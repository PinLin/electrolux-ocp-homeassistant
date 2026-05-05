"""Tests for `_request` HTTP-status → exception mapping.

These guard the 429 / cas_3404 / 401 / 5xx routing assumed by
`__init__.py async_setup_entry`, `coordinator.py _async_update_data`,
and the WS handshake handler. Each maps to a distinct downstream
behaviour (ConfigEntryAuthFailed vs ConfigEntryNotReady vs RateLimit
backoff) so getting the mapping wrong silently miscategorises faults.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.electrolux_ocp.api import (
    ElectroluxApiClient,
    ElectroluxApiError,
    ElectroluxAuthError,
    ElectroluxRateLimitError,
)


class _FakeResponse:
    def __init__(self, status: int, text: str = "", content_type: str = "application/json"):
        self.status = status
        self._text = text
        self.headers = {"Content-Type": content_type}

    async def text(self) -> str:
        return self._text


class _FakeRequestCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *args) -> bool:
        return False


def _client_with_response(response: _FakeResponse) -> ElectroluxApiClient:
    """Build a client whose underlying session returns `response`."""
    session = MagicMock()
    session.request = MagicMock(return_value=_FakeRequestCM(response))
    return ElectroluxApiClient(session=session, api_base_url="https://test.local")


class TestRequestStatusMapping:
    """`_request` must raise the right exception class per HTTP status."""

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit_error(self):
        # Plain 429 from any OCP endpoint must be RateLimitError so callers
        # back off instead of triggering reauth.
        client = _client_with_response(_FakeResponse(429, text="Too Many Requests"))
        with pytest.raises(ElectroluxRateLimitError) as exc:
            await client._request("GET", "/anything")
        assert "429" in str(exc.value)

    @pytest.mark.asyncio
    async def test_cas_3404_in_body_raises_rate_limit_error(self):
        # OCP signals refresh-too-frequent with cas_3404 inside the body, often
        # with a 200 or 4xx outer status. Body inspection must catch it.
        body = '{"errorCode":"cas_3404","message":"Too frequent refresh token request"}'
        client = _client_with_response(_FakeResponse(400, text=body))
        with pytest.raises(ElectroluxRateLimitError):
            await client._request("POST", "/one-account-authorization/api/v1/token")

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        client = _client_with_response(_FakeResponse(401, text="Unauthorized"))
        with pytest.raises(ElectroluxAuthError):
            await client._request("GET", "/appliance/api/v2/appliances")

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        client = _client_with_response(_FakeResponse(403, text="Forbidden"))
        with pytest.raises(ElectroluxAuthError):
            await client._request("GET", "/appliance/api/v2/appliances")

    @pytest.mark.asyncio
    async def test_500_raises_generic_api_error_not_rate_limit(self):
        # Server errors must NOT be miscategorised as rate-limit (which would
        # induce a 5-min OCP backoff for a transient hiccup).
        client = _client_with_response(_FakeResponse(500, text="Internal Server Error"))
        with pytest.raises(ElectroluxApiError) as exc:
            await client._request("GET", "/appliance/api/v2/appliances")
        # Must be the base class instance, not a subclass.
        assert not isinstance(exc.value, ElectroluxRateLimitError)
        assert not isinstance(exc.value, ElectroluxAuthError)

    @pytest.mark.asyncio
    async def test_200_json_passthrough(self):
        # Sanity: success path still parses JSON.
        body = '{"applianceId":"abc","applianceName":"Living"}'
        client = _client_with_response(_FakeResponse(200, text=body))
        result = await client._request("GET", "/test")
        assert result == {"applianceId": "abc", "applianceName": "Living"}

    @pytest.mark.asyncio
    async def test_200_empty_body_returns_none(self):
        # Empty body is the soft-failure mode Gigya guards distinguish; at
        # the _request layer it remains a None passthrough — call sites
        # interpret it (see api.py async_login Gigya guards).
        client = _client_with_response(_FakeResponse(200, text=""))
        result = await client._request("GET", "/test")
        assert result is None
