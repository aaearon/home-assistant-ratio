"""Tests for the Ratio binary sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aioratio.models import ChargerOverview
from homeassistant.core import HomeAssistant

from custom_components.ratio.coordinator import RatioData

SERIAL = "SN001"


def _make_overview(
    serial: str = SERIAL,
    *,
    vehicle_connected: bool = True,
    session_active: bool = False,
    charging_paused: bool = False,
    errors: list[dict] | None = None,
    charging_disabled: bool = False,
    charging_disabled_reason: str | None = None,
    charging_authorized: bool = True,
    power_reduced_by_dso: bool = False,
    charging_state: str = "idle",
    fw_update_available: bool = False,
    fw_update_allowed: bool = False,
) -> ChargerOverview:
    d: dict = {
        "serialNumber": serial,
        "chargerStatus": {
            "indicators": {
                "isVehicleConnected": vehicle_connected,
                "isChargeSessionActive": session_active,
                "isChargingPaused": charging_paused,
                "errors": errors if errors is not None else [],
                "isChargingDisabled": charging_disabled,
                "isChargingDisabledReason": charging_disabled_reason,
                "isChargingAuthorized": charging_authorized,
                "isPowerReducedByDso": power_reduced_by_dso,
                "chargingState": charging_state,
            },
            "isChargeStartAllowed": True,
            "isChargeStopAllowed": False,
        },
        "chargerFirmwareStatus": {
            "isFirmwareUpdateAvailable": fw_update_available,
            "isFirmwareUpdateAllowed": fw_update_allowed,
            "firmwareUpdateJobs": [],
        },
    }
    return ChargerOverview.from_dict(d)


# Mapping from logical key to HA entity_id suffix (derived from entity name).
_KEY_TO_SUFFIX = {
    "vehicle_connected": "vehicle_connected",
    "charge_session_active": "charging",
    "charging_paused": "charging_paused",
    "error": "error",
    "charging_disabled": "charging_disabled",
    "charging_authorized": "charging_authorized",
    "power_reduced_by_dso": "power_reduced_by_dso",
    "firmware_update_available": "firmware_update_available",
    "firmware_update_allowed": "firmware_update_allowed",
}


def _entity_id(key: str, serial: str = SERIAL) -> str:
    suffix = _KEY_TO_SUFFIX.get(key, key)
    return f"binary_sensor.ratio_{serial.lower()}_{suffix}"


@pytest.mark.asyncio
async def test_binary_sensors_created(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """All binary sensors should be created when charger data arrives."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Entity IDs are derived from device name + entity name, not key.
    expected_suffixes = [
        "vehicle_connected",
        "charging",  # charge_session_active -> name "Charging"
        "charging_paused",
        "error",
        "charging_disabled",
        "charging_authorized",
        "power_reduced_by_dso",
        "firmware_update_available",
        "firmware_update_allowed",
    ]
    for suffix in expected_suffixes:
        eid = f"binary_sensor.ratio_{SERIAL.lower()}_{suffix}"
        state = hass.states.get(eid)
        assert state is not None, f"Missing binary_sensor: {eid}"


@pytest.mark.asyncio
async def test_vehicle_connected_on(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Vehicle connected sensor should be 'on' when connected."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(vehicle_connected=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
async def test_vehicle_connected_off(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Vehicle connected sensor should be 'off' when disconnected."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(vehicle_connected=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_error_sensor_on_when_errors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Error sensor should be 'on' when errors list is non-empty."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(errors=[{"errorCode": 1}])
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("error"))
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
async def test_error_sensor_off_when_no_errors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Error sensor should be 'off' when errors list is empty."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(errors=[])
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("error"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_charging_disabled_extra_attributes(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Charging disabled sensor should include reason in extra attributes."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        charging_disabled=True,
        charging_disabled_reason="schedule",
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging_disabled"))
    assert state is not None
    assert state.state == "on"
    assert state.attributes.get("reason") == "schedule"


@pytest.mark.asyncio
async def test_charging_disabled_no_reason_no_extra_attrs(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Charging disabled sensor should have no extra attrs when reason is None."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(charging_disabled=False, charging_disabled_reason=None)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging_disabled"))
    assert state is not None
    assert state.state == "off"
    assert "reason" not in state.attributes


@pytest.mark.asyncio
async def test_binary_sensor_unknown_when_no_data(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Binary sensors should be 'unknown' when coordinator data is None."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    # First create the entities
    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Then set data to None
    coordinator.async_set_updated_data(None)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "unknown"


@pytest.mark.asyncio
async def test_binary_sensor_unknown_when_charger_missing(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Binary sensors should be 'unknown' when charger is removed from data."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Remove the charger
    coordinator.async_set_updated_data(RatioData(chargers={}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "unknown"


@pytest.mark.asyncio
async def test_firmware_binary_sensors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Firmware binary sensors should reflect firmware status."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(fw_update_available=True, fw_update_allowed=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("firmware_update_available"))
    assert state is not None
    assert state.state == "on"

    state = hass.states.get(_entity_id("firmware_update_allowed"))
    assert state is not None
    assert state.state == "off"
