"""Tests for the Ratio coordinator data shape."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import ChargerOverview, UserSettings, Vehicle

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.ratio.coordinator import RatioCoordinator, RatioData


def _overview(serial: str) -> ChargerOverview:
    return ChargerOverview.from_dict({"serialNumber": serial})


@pytest.mark.asyncio
async def test_update_populates_chargers_settings_and_vehicles(
    hass: HomeAssistant,
) -> None:
    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    client.user_settings = AsyncMock(return_value=UserSettings())
    client.vehicles = AsyncMock(
        return_value=[Vehicle(vehicle_id="v1", vehicle_name="Car")]
    )

    entry = MagicMock(spec=ConfigEntry, entry_id="test_entry")
    coord = RatioCoordinator(hass, client, entry)
    data = await coord._async_update_data()

    assert isinstance(data, RatioData)
    assert "ABC123" in data.chargers
    assert "ABC123" in data.user_settings
    assert len(data.vehicles) == 1
    assert data.vehicles[0].vehicle_id == "v1"


@pytest.mark.asyncio
async def test_update_keeps_last_known_settings_on_per_charger_failure(
    hass: HomeAssistant,
) -> None:
    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    from aioratio.exceptions import RatioApiError

    # First cycle succeeds.
    client.user_settings = AsyncMock(return_value=UserSettings())
    client.vehicles = AsyncMock(return_value=[])
    entry = MagicMock(spec=ConfigEntry, entry_id="test_entry")
    coord = RatioCoordinator(hass, client, entry)
    coord.data = await coord._async_update_data()
    cached = coord.data.user_settings["ABC123"]

    # Second cycle: settings call fails — coordinator keeps the prior value.
    client.user_settings = AsyncMock(side_effect=RatioApiError("boom"))
    new_data = await coord._async_update_data()
    assert new_data.user_settings["ABC123"] is cached
