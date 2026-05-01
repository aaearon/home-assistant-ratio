"""Tests for Ratio diagnostics."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aioratio.models import ChargerOverview, Vehicle
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.diagnostics import async_get_config_entry_diagnostics


SERIAL = "SN001"


@pytest.mark.asyncio
async def test_diagnostics_output_shape(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Diagnostics should include entry_data and coordinator_data."""
    entry = setup_integration
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert "entry_data" in result
    assert "coordinator_data" in result
    coord_data = result["coordinator_data"]
    assert "chargers" in coord_data
    assert "user_settings" in coord_data
    assert "solar_settings" in coord_data
    assert "vehicles" in coord_data


@pytest.mark.asyncio
async def test_diagnostics_redacts_sensitive_fields(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Sensitive fields should be redacted."""
    entry = setup_integration
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["entry_data"]["email"] == "**REDACTED**"
    assert result["entry_data"]["password"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_with_charger_data(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Diagnostics should serialize charger data."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = ChargerOverview.from_dict({
        "serialNumber": SERIAL,
        "chargerStatus": {
            "indicators": {
                "isChargeSessionActive": False,
                "isVehicleConnected": True,
                "isChargingPaused": False,
                "errors": [],
                "isChargingDisabled": False,
                "isChargingAuthorized": True,
                "isPowerReducedByDso": False,
                "chargingState": "idle",
            },
            "isChargeStartAllowed": True,
            "isChargeStopAllowed": False,
        },
    })
    vehicles = [Vehicle(vehicle_id="v1", vehicle_name="Tesla")]
    coordinator.async_set_updated_data(
        RatioData(chargers={SERIAL: ov}, vehicles=vehicles)
    )
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)
    coord_data = result["coordinator_data"]
    assert len(coord_data["chargers"]) == 1
    assert len(coord_data["vehicles"]) == 1
    # serial_number should be redacted
    charger = coord_data["chargers"][0]
    assert charger.get("serial_number") == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_empty_data(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Diagnostics should handle empty coordinator data."""
    entry = setup_integration
    # Default setup has empty RatioData
    result = await async_get_config_entry_diagnostics(hass, entry)
    coord_data = result["coordinator_data"]
    assert coord_data["chargers"] == []
    assert coord_data["user_settings"] == []
    assert coord_data["solar_settings"] == []
    assert coord_data["vehicles"] == []
