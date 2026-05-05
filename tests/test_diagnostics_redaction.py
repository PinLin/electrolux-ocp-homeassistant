"""Verify diagnostics redaction strips all sensitive fields.

These tests guard against accidentally exposing tokens, account PII, or
appliance identifiers in user-shared diagnostic dumps.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data

from custom_components.electrolux_ocp.const import REDACT_KEYS


REDACTED = "**REDACTED**"


def _walk_string_values(obj):
    """Yield every leaf string in a nested structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_string_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_string_values(v)


class TestSensitiveKeysRedacted:
    def test_tokens_are_redacted(self):
        payload = {
            "access_token": "TOKEN_MUST_NOT_LEAK",
            "refresh_token": "REFRESH_MUST_NOT_LEAK",
            "api_key": "APIKEY_MUST_NOT_LEAK",
            "password": "P4SSW0RD",
        }
        redacted = async_redact_data(payload, REDACT_KEYS)
        for v in _walk_string_values(redacted):
            assert "MUST_NOT_LEAK" not in v
            assert "P4SSW0RD" not in v

    def test_email_redacted(self):
        payload = {"email": "user@example.com", "userName": "userhandle"}
        redacted = async_redact_data(payload, REDACT_KEYS)
        assert redacted["email"] == REDACTED
        assert redacted["userName"] == REDACTED

    def test_appliance_identifiers_redacted(self, sample_appliance_purea9):
        # The full appliance payload should have its identifiers stripped.
        redacted = async_redact_data(sample_appliance_purea9, REDACT_KEYS)
        assert redacted["applianceId"] == REDACTED
        assert redacted["applianceName"] == REDACTED

    def test_reported_device_id_redacted(self, sample_appliance_purea9):
        redacted = async_redact_data(sample_appliance_purea9, REDACT_KEYS)
        # DeviceId nested inside properties.reported should also be redacted.
        assert redacted["properties"]["reported"]["DeviceId"] == REDACTED

    def test_mac_address_variants_redacted(self):
        payload = {
            "macAddress": "AA:BB:CC:DD:EE:FF",
            "MacAddress": "AA:BB:CC:DD:EE:FF",
            "macaddress": "AA:BB:CC:DD:EE:FF",
            "MAC": "AA:BB:CC:DD:EE:FF",
        }
        redacted = async_redact_data(payload, REDACT_KEYS)
        for key in payload:
            assert redacted[key] == REDACTED

    def test_serial_redacted(self):
        payload = {"serialNumber": "SN12345", "sn": "SN12345", "SN": "SN12345"}
        redacted = async_redact_data(payload, REDACT_KEYS)
        for key in payload:
            assert redacted[key] == REDACTED


class TestNonSensitiveKeysPreserved:
    """Sanity check: redaction shouldn't be over-aggressive on debug data."""

    def test_sensor_values_preserved(self, sample_appliance_purea9):
        redacted = async_redact_data(sample_appliance_purea9, REDACT_KEYS)
        reported = redacted["properties"]["reported"]
        # Numeric measurements should pass through unchanged — these are
        # what a developer needs to debug a misbehaving sensor.
        assert reported["Temp"] == 28
        assert reported["PM2_5"] == 2
        assert reported["TVOC"] == 21
        assert reported["FilterLife"] == 25

    def test_firmware_version_preserved(self, sample_appliance_purea9):
        redacted = async_redact_data(sample_appliance_purea9, REDACT_KEYS)
        # Firmware versions help diagnose vendor-firmware-specific bugs.
        assert redacted["properties"]["reported"]["FrmVer_NIU"] == "4.0.0"

    def test_model_preserved(self, sample_appliance_purea9):
        redacted = async_redact_data(sample_appliance_purea9, REDACT_KEYS)
        # PNC is redacted (it acts as a hardware identifier), but modelName
        # in applianceData identifies the SKU and is needed for triage.
        assert redacted["applianceData"]["modelName"] == "PUREA9"
