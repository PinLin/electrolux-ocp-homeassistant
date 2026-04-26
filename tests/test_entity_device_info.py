"""Tests for entity.device_info — the metadata HA shows on the device card.

The base class extends CoordinatorEntity, but device_info only reads
self._appliance and self._appliance_id — so we sidestep the full init
and stub those directly. That keeps the test focused on metadata
composition without booting HA.
"""

from __future__ import annotations

from custom_components.electrolux.const import DOMAIN
from custom_components.electrolux.entity import ElectroluxBaseEntity


class _StubEntity(ElectroluxBaseEntity):
    """Minimal entity that returns a fixed appliance dict."""

    def __init__(self, appliance, appliance_id: str = "test-id-001"):
        self._appliance_id = appliance_id
        self._stub_appliance = appliance

    @property
    def _appliance(self):
        return self._stub_appliance


def _make_appliance(*, main_fw=None, niu_fw=None, appliance_type=None, name="Living Room"):
    appliance = {
        "applianceId": "test-id-001",
        "applianceName": name,
        "properties": {"reported": {}},
    }
    if main_fw is not None:
        appliance["firmwareVersion"] = main_fw
    if niu_fw is not None:
        appliance["properties"]["reported"]["FrmVer_NIU"] = niu_fw
    if appliance_type is not None:
        appliance["applianceType"] = appliance_type
    return appliance


class TestSwVersion:
    def test_main_firmware_wins_when_both_present(self):
        entity = _StubEntity(_make_appliance(main_fw="1.4.5", niu_fw="4.0.0"))
        assert entity.device_info["sw_version"] == "1.4.5"

    def test_main_only(self):
        entity = _StubEntity(_make_appliance(main_fw="1.4.5"))
        assert entity.device_info["sw_version"] == "1.4.5"

    def test_niu_only(self):
        entity = _StubEntity(_make_appliance(niu_fw="4.0.0"))
        assert entity.device_info["sw_version"] == "4.0.0"

    def test_neither(self):
        entity = _StubEntity(_make_appliance())
        assert entity.device_info["sw_version"] is None


class TestIdentifiersAndCore:
    def test_identifier_uses_appliance_id_under_domain(self):
        entity = _StubEntity(_make_appliance(), appliance_id="abc-123")
        info = entity.device_info
        assert info["identifiers"] == {(DOMAIN, "abc-123")}

    def test_manufacturer_is_electrolux(self):
        entity = _StubEntity(_make_appliance())
        assert entity.device_info["manufacturer"] == "Electrolux"

    def test_manufacturer_uses_appliance_info_brand(self):
        appliance = _make_appliance()
        appliance["applianceInfo"] = {"brand": "AEG"}
        entity = _StubEntity(appliance)
        assert entity.device_info["manufacturer"] == "AEG"

    def test_name_passed_through(self):
        entity = _StubEntity(_make_appliance(name="客廳"))
        assert entity.device_info["name"] == "客廳"

    def test_model_id_uses_pnc(self):
        appliance = _make_appliance()
        appliance["pnc"] = "111222333"
        entity = _StubEntity(appliance)
        assert entity.device_info["model_id"] == "111222333"

    def test_hw_version_is_not_set_from_appliance_type(self):
        entity = _StubEntity(_make_appliance(appliance_type="AIRPURIFIER"))
        assert "hw_version" not in entity.device_info

    def test_hw_version_is_not_set_from_appliance_info_device_type(self):
        appliance = _make_appliance()
        appliance["applianceInfo"] = {"deviceType": "AIR_PURIFIER"}
        entity = _StubEntity(appliance)
        assert "hw_version" not in entity.device_info

    def test_serial_number_uses_factory_serial_only(self):
        appliance = _make_appliance()
        appliance["serialNumber"] = "SN-123"
        appliance["properties"]["reported"]["DeviceId"] = "device-id"
        entity = _StubEntity(appliance)
        assert entity.device_info["serial_number"] == "SN-123"

    def test_serial_number_derives_from_product_identifier(self):
        appliance = _make_appliance()
        appliance["applianceId"] = "111222333444455556667777"
        appliance["pnc"] = "111222333"
        entity = _StubEntity(appliance)
        assert entity.device_info["serial_number"] == "44445555"


class TestApplianceMissing:
    def test_returns_minimal_info_when_appliance_is_none(self):
        # Coordinator hasn't seen the appliance yet (e.g. between unique_id
        # registration and first refresh) — device_info must still produce
        # a valid identifier so HA can register the device.
        entity = _StubEntity(None, appliance_id="pending-001")
        info = entity.device_info
        assert info["identifiers"] == {(DOMAIN, "pending-001")}
        # Optional fields shouldn't be set on the stub-info path.
        assert info.get("manufacturer") is None
