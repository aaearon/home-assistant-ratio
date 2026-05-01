"""Tests for Ratio number entities."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import SolarSettings, UserSettings
from aioratio.models.settings import UpperLowerLimitSetting

from custom_components.ratio.coordinator import RatioData

from custom_components.ratio.number import (
    RatioMaximumChargingCurrentNumber,
    RatioMinimumChargingCurrentNumber,
    RatioPureSolarStartingCurrentNumber,
    RatioSmartSolarStartingCurrentNumber,
    RatioSunOffDelayMinutesNumber,
    RatioSunOnDelayMinutesNumber,
)


SERIAL = "SN001"


def _solar() -> SolarSettings:
    return SolarSettings(
        sun_on_delay_minutes=UpperLowerLimitSetting(value=2.0, lower=0.0, upper=10.0),
        sun_off_delay_minutes=UpperLowerLimitSetting(value=3.0, lower=0.0, upper=15.0),
        pure_solar_starting_current=UpperLowerLimitSetting(value=6.0, lower=6.0, upper=16.0),
        smart_solar_starting_current=UpperLowerLimitSetting(value=8.0, lower=6.0, upper=16.0),
    )


def _user() -> UserSettings:
    return UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(value=16.0, lower=6.0, upper=32.0),
        minimum_charging_current=UpperLowerLimitSetting(value=6.0, lower=6.0, upper=16.0),
    )


def _make_coordinator(
    solar: SolarSettings | None,
    user: UserSettings | None,
    serial: str = SERIAL,
) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(
        solar_settings={serial: solar} if solar is not None else {},
        user_settings={serial: user} if user is not None else {},
    )

    async def _passthrough(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    coord.request_command = AsyncMock(side_effect=_passthrough)
    return coord


# ---- Reads ----

@pytest.mark.parametrize(
    "cls,expected",
    [
        (RatioSunOnDelayMinutesNumber, 2.0),
        (RatioSunOffDelayMinutesNumber, 3.0),
        (RatioPureSolarStartingCurrentNumber, 6.0),
        (RatioSmartSolarStartingCurrentNumber, 8.0),
        (RatioMaximumChargingCurrentNumber, 16.0),
        (RatioMinimumChargingCurrentNumber, 6.0),
    ],
)
def test_native_value_reads_from_settings(cls, expected) -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    entity = cls(coord, client, SERIAL)
    assert entity.native_value == expected
    assert entity.available is True


@pytest.mark.parametrize(
    "cls,lo,hi",
    [
        (RatioSunOnDelayMinutesNumber, 0.0, 10.0),
        (RatioSunOffDelayMinutesNumber, 0.0, 15.0),
        (RatioPureSolarStartingCurrentNumber, 6.0, 16.0),
        (RatioSmartSolarStartingCurrentNumber, 6.0, 16.0),
        (RatioMaximumChargingCurrentNumber, 6.0, 32.0),
        (RatioMinimumChargingCurrentNumber, 6.0, 16.0),
    ],
)
def test_min_max_from_lower_upper(cls, lo, hi) -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    entity = cls(coord, client, SERIAL)
    assert entity.native_min_value == lo
    assert entity.native_max_value == hi


def test_unavailable_when_no_settings_for_serial() -> None:
    coord = _make_coordinator(None, None)
    client = MagicMock()

    for cls in (
        RatioSunOnDelayMinutesNumber,
        RatioSunOffDelayMinutesNumber,
        RatioPureSolarStartingCurrentNumber,
        RatioSmartSolarStartingCurrentNumber,
    ):
        e = cls(coord, client, SERIAL)
        assert e.native_value is None
        assert e.available is False

    for cls in (
        RatioMaximumChargingCurrentNumber,
        RatioMinimumChargingCurrentNumber,
    ):
        e = cls(coord, client, SERIAL)
        assert e.native_value is None
        assert e.available is False


# ---- Writes ----

@pytest.mark.asyncio
async def test_set_solar_field_preserves_other_fields() -> None:
    solar = _solar()
    coord = _make_coordinator(solar, _user())
    client = MagicMock()
    client.set_solar_settings = AsyncMock()
    client.set_user_settings = AsyncMock()

    entity = RatioSunOnDelayMinutesNumber(coord, client, SERIAL)
    await entity.async_set_native_value(7.0)

    client.set_solar_settings.assert_awaited_once()
    args, _ = client.set_solar_settings.call_args
    assert args[0] == SERIAL
    new_settings: SolarSettings = args[1]
    assert isinstance(new_settings, SolarSettings)
    # Changed field
    assert new_settings.sun_on_delay_minutes.value == 7.0
    # Preserve bounds on changed field
    assert new_settings.sun_on_delay_minutes.lower == 0.0
    assert new_settings.sun_on_delay_minutes.upper == 10.0
    # Other fields preserved
    assert new_settings.sun_off_delay_minutes == solar.sun_off_delay_minutes
    assert new_settings.pure_solar_starting_current == solar.pure_solar_starting_current
    assert new_settings.smart_solar_starting_current == solar.smart_solar_starting_current
    client.set_user_settings.assert_not_called()


@pytest.mark.asyncio
async def test_set_user_field_preserves_other_fields() -> None:
    user = _user()
    coord = _make_coordinator(_solar(), user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()
    client.set_solar_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(20.0)

    client.set_user_settings.assert_awaited_once()
    args, _ = client.set_user_settings.call_args
    assert args[0] == SERIAL
    new_settings: UserSettings = args[1]
    assert isinstance(new_settings, UserSettings)
    assert new_settings.maximum_charging_current.value == 20.0
    assert new_settings.maximum_charging_current.lower == 6.0
    assert new_settings.maximum_charging_current.upper == 32.0
    # Other field preserved
    assert new_settings.minimum_charging_current == user.minimum_charging_current
    client.set_solar_settings.assert_not_called()


@pytest.mark.asyncio
async def test_request_command_used_for_writes() -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_solar_settings = AsyncMock()

    entity = RatioPureSolarStartingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(10.0)

    coord.request_command.assert_awaited_once()
