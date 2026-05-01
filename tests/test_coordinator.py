"""Tests for the Ratio coordinator data shape."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import ChargerOverview, SolarSettings, UserSettings, Vehicle
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import RatioCoordinator, RatioData


def _overview(serial: str) -> ChargerOverview:
    return ChargerOverview.from_dict({"serialNumber": serial})


def _make_entry(hass: HomeAssistant, entry_id: str = "test_entry") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    return entry


@pytest.mark.asyncio
async def test_update_populates_chargers_settings_and_vehicles(
    hass: HomeAssistant,
) -> None:
    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    client.user_settings = AsyncMock(return_value=UserSettings())
    client.solar_settings = AsyncMock(return_value=SolarSettings())
    client.vehicles = AsyncMock(
        return_value=[Vehicle(vehicle_id="v1", vehicle_name="Car")]
    )

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()

    assert isinstance(coord.data, RatioData)
    assert "ABC123" in coord.data.chargers
    assert "ABC123" in coord.data.user_settings
    assert "ABC123" in coord.data.solar_settings
    assert len(coord.data.vehicles) == 1
    assert coord.data.vehicles[0].vehicle_id == "v1"


@pytest.mark.asyncio
async def test_update_keeps_last_known_solar_on_per_charger_failure(
    hass: HomeAssistant,
) -> None:
    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    from aioratio.exceptions import RatioApiError

    client.user_settings = AsyncMock(return_value=UserSettings())
    client.solar_settings = AsyncMock(return_value=SolarSettings())
    client.vehicles = AsyncMock(return_value=[])
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()
    cached = coord.data.solar_settings["ABC123"]

    # Second cycle: solar call fails — coordinator keeps the prior value.
    client.solar_settings = AsyncMock(side_effect=RatioApiError("boom"))
    await coord.async_refresh()
    assert coord.last_update_success is True
    assert coord.data.solar_settings["ABC123"] is cached


@pytest.mark.asyncio
async def test_update_keeps_last_known_settings_on_per_charger_failure(
    hass: HomeAssistant,
) -> None:
    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    from aioratio.exceptions import RatioApiError

    # First cycle succeeds.
    client.user_settings = AsyncMock(return_value=UserSettings())
    client.solar_settings = AsyncMock(return_value=SolarSettings())
    client.vehicles = AsyncMock(return_value=[])
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()
    cached = coord.data.user_settings["ABC123"]

    # Second cycle: settings call fails — coordinator keeps the prior value.
    client.user_settings = AsyncMock(side_effect=RatioApiError("boom"))
    await coord.async_refresh()
    assert coord.last_update_success is True
    assert coord.data.user_settings["ABC123"] is cached


@pytest.mark.asyncio
async def test_preferred_vehicle_persists_across_reload(
    hass: HomeAssistant,
) -> None:
    """preferred_vehicle should round-trip through the HA Store."""
    client = MagicMock()
    entry = _make_entry(hass, entry_id="persist_entry")

    # First "session": set preference and save.
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_load_preferences()
    assert coord.preferred_vehicle == {}
    coord.preferred_vehicle["ABC123"] = "v42"
    await coord.async_save_preferences()

    # Simulate reload — fresh coordinator instance, same entry_id, same hass.
    coord2 = RatioCoordinator(hass, client, entry)
    await coord2.async_load_preferences()
    assert coord2.preferred_vehicle == {"ABC123": "v42"}


@pytest.mark.asyncio
async def test_update_raises_config_entry_auth_failed_on_auth_error(
    hass: HomeAssistant,
) -> None:
    """Auth error during chargers_overview should raise ConfigEntryAuthFailed."""
    from aioratio.exceptions import RatioAuthError
    from homeassistant.exceptions import ConfigEntryAuthFailed

    client = MagicMock()
    client.chargers_overview = AsyncMock(
        side_effect=RatioAuthError("expired token")
    )
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_raises_update_failed_on_connection_error(
    hass: HomeAssistant,
) -> None:
    """Connection error during chargers_overview should raise UpdateFailed."""
    from aioratio.exceptions import RatioConnectionError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.chargers_overview = AsyncMock(
        side_effect=RatioConnectionError("timeout")
    )
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_raises_update_failed_on_rate_limit(
    hass: HomeAssistant,
) -> None:
    """Rate limit during chargers_overview should raise UpdateFailed."""
    from aioratio.exceptions import RatioRateLimitError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.chargers_overview = AsyncMock(
        side_effect=RatioRateLimitError("429")
    )
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed, match="rate limited"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_raises_update_failed_on_api_error(
    hass: HomeAssistant,
) -> None:
    """API error during chargers_overview should raise UpdateFailed."""
    from aioratio.exceptions import RatioApiError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.chargers_overview = AsyncMock(
        side_effect=RatioApiError("500")
    )
    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_request_command_rate_limit_error(
    hass: HomeAssistant,
) -> None:
    """request_command should raise HomeAssistantError on RatioRateLimitError."""
    from aioratio.exceptions import RatioRateLimitError
    from homeassistant.exceptions import HomeAssistantError

    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[])
    client.user_settings = AsyncMock(return_value=None)
    client.solar_settings = AsyncMock(return_value=None)
    client.vehicles = AsyncMock(return_value=[])

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()

    failing_fn = AsyncMock(side_effect=RatioRateLimitError("429"))

    with pytest.raises(HomeAssistantError):
        await coord.request_command(failing_fn, "SN001")


@pytest.mark.asyncio
async def test_request_command_connection_error(
    hass: HomeAssistant,
) -> None:
    """request_command should raise HomeAssistantError on RatioConnectionError."""
    from aioratio.exceptions import RatioConnectionError
    from homeassistant.exceptions import HomeAssistantError

    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[])
    client.user_settings = AsyncMock(return_value=None)
    client.solar_settings = AsyncMock(return_value=None)
    client.vehicles = AsyncMock(return_value=[])

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()

    failing_fn = AsyncMock(side_effect=RatioConnectionError("timeout"))

    with pytest.raises(HomeAssistantError):
        await coord.request_command(failing_fn, "SN001")


@pytest.mark.asyncio
async def test_request_command_api_error(
    hass: HomeAssistant,
) -> None:
    """request_command should raise HomeAssistantError on RatioApiError."""
    from aioratio.exceptions import RatioApiError
    from homeassistant.exceptions import HomeAssistantError

    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[])
    client.user_settings = AsyncMock(return_value=None)
    client.solar_settings = AsyncMock(return_value=None)
    client.vehicles = AsyncMock(return_value=[])

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()

    failing_fn = AsyncMock(side_effect=RatioApiError("server error"))

    with pytest.raises(HomeAssistantError):
        await coord.request_command(failing_fn, "SN001")


@pytest.mark.asyncio
async def test_update_keeps_last_known_vehicles_on_failure(
    hass: HomeAssistant,
) -> None:
    """Vehicles should be preserved from previous cycle on failure."""
    from aioratio.exceptions import RatioApiError

    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    client.user_settings = AsyncMock(return_value=None)
    client.solar_settings = AsyncMock(return_value=None)
    client.vehicles = AsyncMock(
        return_value=[Vehicle(vehicle_id="v1", vehicle_name="Car")]
    )

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)
    await coord.async_config_entry_first_refresh()
    assert len(coord.data.vehicles) == 1

    # Second cycle: vehicles call fails
    client.vehicles = AsyncMock(side_effect=RatioApiError("boom"))
    await coord.async_refresh()
    assert coord.last_update_success is True
    assert len(coord.data.vehicles) == 1
    assert coord.data.vehicles[0].vehicle_id == "v1"


@pytest.mark.asyncio
async def test_update_rate_limit_in_parallel_settings(
    hass: HomeAssistant,
) -> None:
    """Rate limit in parallel settings fetch should raise UpdateFailed."""
    from aioratio.exceptions import RatioRateLimitError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.chargers_overview = AsyncMock(return_value=[_overview("ABC123")])
    client.user_settings = AsyncMock(
        side_effect=RatioRateLimitError("429")
    )
    client.solar_settings = AsyncMock(return_value=None)
    client.vehicles = AsyncMock(return_value=[])

    entry = _make_entry(hass)
    coord = RatioCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed, match="rate limited"):
        await coord._async_update_data()
