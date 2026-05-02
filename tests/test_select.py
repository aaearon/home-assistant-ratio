"""Tests for Ratio select entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import UserSettings, Vehicle
from aioratio.models.settings import EnumValue

from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.select import (
    RatioActiveVehicleSelect,
    RatioChargeModeSelect,
)

SERIAL = "SN001"


def _make_coordinator(
    vehicles: list[Vehicle] | None = None,
    user_settings: dict | None = None,
) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(
        vehicles=vehicles or [],
        user_settings=user_settings or {},
    )
    coord.preferred_vehicle = {}
    coord.async_save_preferences = AsyncMock()
    return coord


# ---- ActiveVehicleSelect unit tests ----


def test_duplicate_vehicle_names_produce_unique_options() -> None:
    """Two vehicles with the same name should get disambiguated options."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="My Car"),
        Vehicle(vehicle_id="v2", vehicle_name="My Car"),
    ]
    coord = _make_coordinator(vehicles=vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, "SN001")

    opts = entity.options
    assert len(opts) == 2
    assert len(set(opts)) == 2  # all unique
    assert "My Car (v1)" in opts
    assert "My Car (v2)" in opts


def test_unique_vehicle_names_not_disambiguated() -> None:
    """Vehicles with distinct names should not get IDs appended."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="Tesla"),
        Vehicle(vehicle_id="v2", vehicle_name="BMW"),
    ]
    coord = _make_coordinator(vehicles=vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, "SN001")

    opts = entity.options
    assert opts == ["Tesla", "BMW"]


# ---- ChargeModeSelect unit tests ----


def test_charge_mode_options_from_settings() -> None:
    """Options should come from user settings charging_mode allowed_values."""
    us = UserSettings(
        charging_mode=EnumValue(
            value="Smart",
            allowed_values=["Smart", "SmartSolar", "PureSolar"],
        ),
    )
    coord = _make_coordinator(user_settings={SERIAL: us})
    client = MagicMock()

    entity = RatioChargeModeSelect(coord, client, SERIAL)
    assert entity.options == ["Smart", "SmartSolar", "PureSolar"]
    assert entity.current_option == "Smart"


def test_charge_mode_fallback_options_when_no_settings() -> None:
    """Options should fall back when no user settings exist."""
    coord = _make_coordinator()
    client = MagicMock()

    entity = RatioChargeModeSelect(coord, client, SERIAL)
    assert entity.options == ["Smart", "SmartSolar", "PureSolar"]
    assert entity.current_option is None


def test_charge_mode_fallback_when_data_is_none() -> None:
    """Options should fall back when coordinator data is None."""
    coord = MagicMock()
    coord.data = None
    client = MagicMock()

    entity = RatioChargeModeSelect(coord, client, SERIAL)
    assert entity.options == ["Smart", "SmartSolar", "PureSolar"]
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_charge_mode_select_option() -> None:
    """Selecting a charge mode option should call set_user_settings."""
    us = UserSettings(
        charging_mode=EnumValue(
            value="Smart",
            allowed_values=["Smart", "SmartSolar", "PureSolar"],
        ),
    )
    coord = _make_coordinator(user_settings={SERIAL: us})
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    async def _passthrough(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    coord.request_command = AsyncMock(side_effect=_passthrough)

    entity = RatioChargeModeSelect(coord, client, SERIAL)
    await entity.async_select_option("SmartSolar")

    coord.request_command.assert_awaited_once()
    client.set_user_settings.assert_awaited_once_with(
        SERIAL, {"chargingMode": "SmartSolar"}
    )


# ---- ActiveVehicleSelect async test ----


@pytest.mark.asyncio
async def test_active_vehicle_select_option_saves_preference() -> None:
    """Selecting a vehicle should update preferred_vehicle and save."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="Tesla"),
        Vehicle(vehicle_id="v2", vehicle_name="BMW"),
    ]
    coord = _make_coordinator(vehicles=vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, SERIAL)
    entity.async_write_ha_state = MagicMock()

    await entity.async_select_option("BMW")

    assert coord.preferred_vehicle[SERIAL] == "v2"
    coord.async_save_preferences.assert_awaited_once()
    entity.async_write_ha_state.assert_called_once()


def test_active_vehicle_current_option_from_preferred() -> None:
    """current_option should reflect preferred_vehicle when set."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="Tesla"),
    ]
    coord = _make_coordinator(vehicles=vehicles)
    coord.preferred_vehicle = {SERIAL: "v1"}
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, SERIAL)
    assert entity.current_option == "Tesla"


def test_active_vehicle_current_option_none_when_no_data() -> None:
    """current_option should be None when no data."""
    coord = MagicMock()
    coord.data = None
    coord.preferred_vehicle = {}
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, SERIAL)
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_active_vehicle_select_unknown_option_logs_warning() -> None:
    """Selecting an unknown option should log a warning and not crash."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="Tesla"),
    ]
    coord = _make_coordinator(vehicles=vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, SERIAL)
    entity.async_write_ha_state = MagicMock()

    # This should not raise, just log
    await entity.async_select_option("NonExistent")
    assert SERIAL not in coord.preferred_vehicle
