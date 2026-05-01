"""Tests for firmware diagnostic sensors and grant-permission button (C4, C5)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import ChargerOverview
from aioratio.models.charger import ChargerFirmwareStatus, FirmwareUpdateJob

from custom_components.ratio.coordinator import RatioData

from custom_components.ratio.binary_sensor import (
    FIRMWARE_BINARY_SENSOR_DESCRIPTIONS,
    RatioBinarySensor,
)
from custom_components.ratio.button import RatioGrantUpgradePermissionButton
from custom_components.ratio.sensor import (
    FIRMWARE_SENSOR_DESCRIPTIONS,
    RatioSensor,
)


def _coord(charger: ChargerOverview) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(chargers={charger.serial_number: charger})
    coord.last_update_success = True
    coord.request_command = AsyncMock()
    return coord


def _firmware_binary(coord, serial, key) -> RatioBinarySensor:
    desc = next(d for d in FIRMWARE_BINARY_SENSOR_DESCRIPTIONS if d.key == key)
    return RatioBinarySensor(coord, serial, desc)


def _firmware_sensor(coord, serial, key) -> RatioSensor:
    desc = next(d for d in FIRMWARE_SENSOR_DESCRIPTIONS if d.key == key)
    return RatioSensor(coord, serial, desc)


def test_firmware_binary_sensors_map_when_present() -> None:
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=False,
        firmware_update_status="awaitingPermission",
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)

    assert _firmware_binary(coord, "SN001", "firmware_update_available").is_on is True
    assert _firmware_binary(coord, "SN001", "firmware_update_allowed").is_on is False
    assert _firmware_sensor(coord, "SN001", "firmware_update_status").native_value == "awaitingPermission"


def test_firmware_entities_when_status_absent_return_none() -> None:
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=None)
    coord = _coord(ov)
    assert _firmware_binary(coord, "SN001", "firmware_update_available").is_on is None
    assert _firmware_binary(coord, "SN001", "firmware_update_allowed").is_on is None
    assert _firmware_sensor(coord, "SN001", "firmware_update_status").native_value is None


def test_firmware_status_sensor_disabled_by_default() -> None:
    desc = next(d for d in FIRMWARE_SENSOR_DESCRIPTIONS if d.key == "firmware_update_status")
    assert desc.entity_registry_enabled_default is False


def test_grant_button_available_only_when_update_pending_with_jobs() -> None:
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=False,
        firmware_update_jobs=[FirmwareUpdateJob(job_id="j1")],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    btn = RatioGrantUpgradePermissionButton(coord, MagicMock(), "SN001")
    assert btn.available is True


def test_grant_button_unavailable_when_already_allowed() -> None:
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=True,
        firmware_update_jobs=[FirmwareUpdateJob(job_id="j1")],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    btn = RatioGrantUpgradePermissionButton(coord, MagicMock(), "SN001")
    assert btn.available is False


def test_grant_button_unavailable_without_jobs() -> None:
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=False,
        firmware_update_jobs=[],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    btn = RatioGrantUpgradePermissionButton(coord, MagicMock(), "SN001")
    assert btn.available is False


def test_grant_button_unavailable_when_status_absent() -> None:
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=None)
    coord = _coord(ov)
    btn = RatioGrantUpgradePermissionButton(coord, MagicMock(), "SN001")
    assert btn.available is False


@pytest.mark.asyncio
async def test_grant_button_press_calls_client_with_job_ids() -> None:
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=False,
        firmware_update_jobs=[
            FirmwareUpdateJob(job_id="j1"),
            FirmwareUpdateJob(job_id="j2"),
            FirmwareUpdateJob(job_id=None),  # filtered out
        ],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    client = MagicMock()
    client.grant_upgrade_permission = AsyncMock()
    btn = RatioGrantUpgradePermissionButton(coord, client, "SN001")

    await btn.async_press()

    coord.request_command.assert_awaited_once()
    args, kwargs = coord.request_command.call_args
    assert args[0] is client.grant_upgrade_permission
    assert args[1] == "SN001"
    assert kwargs.get("firmware_update_job_ids") == ["j1", "j2"]


@pytest.mark.asyncio
async def test_grant_button_press_noop_when_no_jobs() -> None:
    """Pressing the button with no jobs should be a no-op."""
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=True,
        is_firmware_update_allowed=False,
        firmware_update_jobs=[],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    client = MagicMock()
    btn = RatioGrantUpgradePermissionButton(coord, client, "SN001")

    await btn.async_press()

    coord.request_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_button_press_noop_when_firmware_status_absent() -> None:
    """Pressing the button with no firmware status should be a no-op."""
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=None)
    coord = _coord(ov)
    client = MagicMock()
    btn = RatioGrantUpgradePermissionButton(coord, client, "SN001")

    await btn.async_press()

    coord.request_command.assert_not_awaited()


def test_grant_button_unavailable_when_no_update_available() -> None:
    """Button should be unavailable when no firmware update is available."""
    fw = ChargerFirmwareStatus(
        is_firmware_update_available=False,
        is_firmware_update_allowed=False,
        firmware_update_jobs=[FirmwareUpdateJob(job_id="j1")],
    )
    ov = ChargerOverview(serial_number="SN001", charger_firmware_status=fw)
    coord = _coord(ov)
    btn = RatioGrantUpgradePermissionButton(coord, MagicMock(), "SN001")
    assert btn.available is False
