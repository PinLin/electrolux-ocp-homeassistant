"""Shared fixtures for the Electrolux integration tests."""

from __future__ import annotations

import pytest

# pytest-homeassistant-custom-component provides the `hass` fixture and
# related HA test infrastructure. Loaded here so individual tests don't
# need to repeat the import.
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
def sample_user() -> dict:
    """Minimal user payload modelled after the OCP /currentUser response."""
    return {
        "userId": "user-abc-123",
        "userName": "sample-user",
        "email": "user@example.com",
    }


@pytest.fixture
def sample_appliance_purea9() -> dict:
    """A trimmed-down Pure A9 appliance payload, similar to what the
    coordinator stores after combining /appliances and /state responses.

    Field casing matches what the OCP gateway actually returns so the
    extractors in api.py exercise the real key paths.
    """
    return {
        "applianceId": "111222333444455556667777",
        "pnc": "111222333",
        "applianceName": "Living room air purifier",
        "applianceData": {
            "applianceName": "Living room air purifier",
            "modelName": "PUREA9",
        },
        "metadata": {
            "online": True,
        },
        "status": "online",
        "connectionState": "Connected",
        "properties": {
            "reported": {
                "Temp": 28,
                "Humidity": 63,
                "PM1": 2,
                "PM2_5": 2,
                "PM10": 2,
                "ECO2": 540,
                "TVOC": 21,
                "FilterLife": 25,
                "RSSI": -45,
                "SignalStrength": "EXCELLENT",
                "Workmode": "Auto",
                "Fanspeed": 3,
                "DoorOpen": False,
                "ErrPM2_5": False,
                "UILight": True,
                "SafetyLock": False,
                "Ionizer": True,
                "DeviceId": "111222333444455556667777",
                "FrmVer_NIU": "4.0.0",
                "VmNo_NIU": "VM000_A_04.00.00_SAMPLE",
                "InterfaceVer": 7,
                "$Version": 12,
            },
        },
    }
