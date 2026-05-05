"""Unit tests for the pure-function extractors in api.py."""

from __future__ import annotations

from custom_components.electrolux_ocp.api import (
    _attach_appliance_metadata,
    extract_appliance_id,
    extract_appliance_manufacturer,
    extract_appliance_model,
    extract_appliance_model_id,
    extract_appliance_name,
    extract_appliance_serial,
    extract_appliance_type,
    extract_firmware_version,
    extract_state_value,
    summarize_appliance,
)


class TestApplianceId:
    def test_top_level(self):
        assert extract_appliance_id({"applianceId": "abc"}) == "abc"

    def test_fallback_to_id(self):
        assert extract_appliance_id({"id": "xyz"}) == "xyz"

    def test_nested_appliance_data(self):
        assert extract_appliance_id({"applianceData": {"applianceId": "n"}}) == "n"

    def test_priority_order(self):
        # Top-level applianceId wins over nested fallbacks.
        payload = {
            "applianceId": "primary",
            "id": "secondary",
            "applianceData": {"applianceId": "tertiary"},
        }
        assert extract_appliance_id(payload) == "primary"

    def test_missing_returns_none(self):
        assert extract_appliance_id({}) is None


class TestApplianceName:
    def test_top_level(self):
        assert extract_appliance_name({"applianceName": "Living Room"}) == "Living Room"

    def test_falls_back_to_id_when_no_name(self):
        assert extract_appliance_name({"applianceId": "abc"}) == "abc"

    def test_default_string_when_nothing(self):
        assert extract_appliance_name({}) == "Electrolux appliance"


class TestApplianceSerial:
    def test_serial_number_field(self):
        assert extract_appliance_serial({"serialNumber": "SN-1"}) == "SN-1"

    def test_sn_field(self):
        assert extract_appliance_serial({"sn": "SN-2"}) == "SN-2"

    def test_nested_sn_field(self):
        assert extract_appliance_serial({"applianceData": {"sn": "SN-3"}}) == "SN-3"

    def test_derives_serial_from_product_identifier_when_pnc_present(self):
        appliance = {
            "applianceId": "111222333444455556667777",
            "pnc": "111222333",
        }
        assert extract_appliance_serial(appliance) == "44445555"

    def test_derives_serial_from_product_identifier_without_pnc(self):
        assert (
            extract_appliance_serial({"applianceId": "111222333444455556667777"})
            == "44445555"
        )

    def test_derives_serial_from_product_identifier_with_metadata_pnc(self):
        appliance = {
            "applianceId": "111222333444455556667777",
            "applianceInfo": {"pnc": "111222333"},
        }
        assert extract_appliance_serial(appliance) == "44445555"

    def test_returns_none_when_product_identifier_cannot_be_split(self):
        assert extract_appliance_serial({"applianceId": "abc", "pnc": "111222333"}) is None

    def test_factory_serial_used_when_present(self, sample_appliance_purea9):
        sample_appliance_purea9["sn"] = "REAL-SERIAL"
        assert extract_appliance_serial(sample_appliance_purea9) == "REAL-SERIAL"

    def test_missing_returns_none(self):
        assert extract_appliance_serial({}) is None


class TestApplianceType:
    def test_top_level_type(self):
        assert extract_appliance_type({"applianceType": "AIRPURIFIER"}) == "AIRPURIFIER"

    def test_appliance_info_device_type(self):
        assert (
            extract_appliance_type({"applianceInfo": {"deviceType": "AIR_PURIFIER"}})
            == "AIR_PURIFIER"
        )

    def test_missing(self):
        assert extract_appliance_type({}) is None


class TestApplianceManufacturer:
    def test_defaults_to_electrolux(self):
        assert extract_appliance_manufacturer({}) == "Electrolux"

    def test_appliance_info_brand(self):
        assert extract_appliance_manufacturer({"applianceInfo": {"brand": "AEG"}}) == "AEG"


class TestApplianceModel:
    def test_pnc_used_as_model(self):
        assert extract_appliance_model({"pnc": "PUREA9"}) == "PUREA9"

    def test_model_field_priority(self):
        assert extract_appliance_model({"model": "X", "pnc": "Y"}) == "X"

    def test_appliance_info_model_before_pnc(self):
        assert (
            extract_appliance_model({"pnc": "914550543", "applianceInfo": {"model": "7000 Series"}})
            == "7000 Series"
        )


class TestApplianceModelId:
    def test_pnc_used_as_model_id(self):
        assert extract_appliance_model_id({"pnc": "111222333"}) == "111222333"

    def test_appliance_info_pnc(self):
        assert extract_appliance_model_id({"applianceInfo": {"pnc": "914550543"}}) == "914550543"

    def test_derives_pnc_from_product_identifier(self):
        assert (
            extract_appliance_model_id({"applianceId": "111222333444455556667777"})
            == "111222333"
        )


class TestFirmwareVersion:
    def test_firmware_version_field(self):
        assert extract_firmware_version({"firmwareVersion": "1.2.3"}) == "1.2.3"

    def test_metadata_nested(self):
        assert extract_firmware_version({"metadata": {"firmwareVersion": "1.0"}}) == "1.0"


class TestStateValue:
    def test_bool_online_true(self):
        assert extract_state_value({"online": True}) == "online"

    def test_bool_online_false(self):
        assert extract_state_value({"online": False}) == "offline"

    def test_string_connected(self):
        assert extract_state_value({"connectionState": "Connected"}) == "online"

    def test_string_disconnected(self):
        assert extract_state_value({"connectionState": "Disconnected"}) == "offline"

    def test_metadata_online_bool(self):
        assert extract_state_value({"metadata": {"online": True}}) == "online"

    def test_unknown_when_no_signal(self):
        assert extract_state_value({}) == "unknown"

    def test_passthrough_status(self):
        # Non-canonical state strings pass through verbatim.
        assert extract_state_value({"state": "Running"}) == "Running"


class TestSummarizeAppliance:
    def test_includes_canonical_fields(self, sample_appliance_purea9):
        summary = summarize_appliance(sample_appliance_purea9)
        assert summary["appliance_id"] == "111222333444455556667777"
        assert summary["name"] == "Living room air purifier"
        assert summary["model"] == "PUREA9"
        assert summary["model_id"] == "111222333"
        assert summary["manufacturer"] == "Electrolux"
        assert summary["state"] == "online"
        assert summary["serial_number"] == "44445555"


class TestAttachApplianceMetadata:
    def test_merges_appliance_info_by_pnc(self):
        payload = {
            "body": {
                "applianceDataResults": [
                    {
                        "pnc": "111222333",
                        "applianceId": "abc",
                        "applianceData": {"modelName": "PUREA9"},
                    }
                ],
                "metaDataResult": [
                    {
                        "pnc": "111222333",
                        "applianceInfoResult": {
                            "pnc": "111222333",
                            "brand": "ELECTROLUX",
                            "deviceType": "AIR_PURIFIER",
                            "model": "A9",
                        },
                        "productCardResult": {"imageUrl": "https://example.test/image.svg"},
                    }
                ],
            }
        }

        appliances = payload["body"]["applianceDataResults"]
        enriched = _attach_appliance_metadata(payload, appliances)

        assert enriched[0]["applianceInfo"]["brand"] == "ELECTROLUX"
        assert enriched[0]["applianceInfo"]["deviceType"] == "AIR_PURIFIER"
        assert enriched[0]["productCard"]["imageUrl"] == "https://example.test/image.svg"
