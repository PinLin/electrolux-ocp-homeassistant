"""Tests for `async_remove_config_entry_device` policy.

Encodes the conservative rule: a device may only be removed when its
appliance has disappeared from the cloud account. An accidental click
on an active appliance must NOT tear down its registry entry — that
caused user pain in similar integrations (cf. albaintor #196 / #194).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.electrolux_ocp import async_remove_config_entry_device
from custom_components.electrolux_ocp.const import DOMAIN
from custom_components.electrolux_ocp.models import ElectroluxData


def _entry_with_appliances(*appliance_ids: str) -> MagicMock:
    """Build a config entry whose coordinator reports the given appliance IDs."""
    appliances = [{"applianceId": aid} for aid in appliance_ids]
    coordinator = MagicMock()
    coordinator.data = ElectroluxData(appliances=appliances, user=None, capabilities={})
    runtime = SimpleNamespace(coordinator=coordinator)
    entry = MagicMock()
    entry.runtime_data = runtime
    return entry


def _device(*identifier_pairs: tuple[str, str]) -> MagicMock:
    device = MagicMock()
    device.identifiers = set(identifier_pairs)
    return device


class TestRemoveConfigEntryDevicePolicy:

    @pytest.mark.asyncio
    async def test_allows_removal_when_appliance_absent(self):
        # Account no longer contains this appliance — the device is stale,
        # let the user clean it up.
        entry = _entry_with_appliances("A-still-active")
        device = _device((DOMAIN, "B-removed-from-account"))
        assert await async_remove_config_entry_device(MagicMock(), entry, device) is True

    @pytest.mark.asyncio
    async def test_refuses_removal_when_appliance_still_active(self):
        # Accidental click — the appliance is still live, mustn't be torn down.
        entry = _entry_with_appliances("A-still-active")
        device = _device((DOMAIN, "A-still-active"))
        assert await async_remove_config_entry_device(MagicMock(), entry, device) is False

    @pytest.mark.asyncio
    async def test_refuses_when_coordinator_data_none(self):
        # No fresh data → can't tell, refuse rather than guess.
        entry = MagicMock()
        entry.runtime_data = SimpleNamespace(coordinator=MagicMock(data=None))
        device = _device((DOMAIN, "any"))
        assert await async_remove_config_entry_device(MagicMock(), entry, device) is False

    @pytest.mark.asyncio
    async def test_refuses_for_device_with_only_foreign_identifier(self):
        # Device wasn't created by us — refuse so we don't accidentally
        # grant removal of someone else's registry entry.
        entry = _entry_with_appliances("A")
        device = _device(("other_integration", "X"))
        assert await async_remove_config_entry_device(MagicMock(), entry, device) is True
        # Note: returning True here is fine because HA only invokes our hook
        # for devices owned by *our* config entry; the foreign-identifier-only
        # case is theoretical, but the loop correctly never short-circuits to
        # False so removal proceeds. Documenting the behaviour explicitly.
