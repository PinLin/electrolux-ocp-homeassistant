"""Concurrency guard for ``async_refresh_token``.

Two callers (REST poll + WS handshake recovery) can race into a refresh at
the same time. The refresh token may be single-use/rotating, so the second
in-flight request would send an already-consumed refresh token and get
invalid_grant → 401 → a spurious reauth. The client must serialise refreshes
and coalesce a concurrent caller onto the first one's result instead of
firing a second HTTP request.
"""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import MagicMock

from custom_components.electrolux_ocp.api import ElectroluxApiClient


def _client() -> ElectroluxApiClient:
    return ElectroluxApiClient(
        session=MagicMock(),
        api_base_url="https://test.local",
        refresh_token="ref-0",
        api_key="key",
    )


@pytest.mark.asyncio
async def test_concurrent_refresh_fires_single_http_request() -> None:
    client = _client()
    calls = 0

    async def fake_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        # Hold the "HTTP" open long enough for the second caller to contend.
        await asyncio.sleep(0.02)
        return {
            "accessToken": "new-access",
            "refreshToken": "ref-1",
            "expiresIn": 3600,
        }

    client._request = fake_request  # type: ignore[method-assign]

    # Both callers must return successfully (no exception, no throttle).
    await asyncio.gather(
        client.async_refresh_token(),
        client.async_refresh_token(),
    )

    assert calls == 1, f"expected a single refresh HTTP request, got {calls}"
    assert client.access_token == "new-access"
    assert client.refresh_token == "ref-1"
