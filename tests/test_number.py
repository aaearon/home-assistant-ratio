"""Tests for Ratio number entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import (
    SolarSettings,
    SolarSettingsUpdate,
    UserSettings,
    UserSettingsUpdate,
)
from aioratio.models.settings import UpperLowerLimitSetting
from homeassistant.exceptions import HomeAssistantError

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
        pure_solar_starting_current=UpperLowerLimitSetting(
            value=6.0, lower=6.0, upper=16.0
        ),
        smart_solar_starting_current=UpperLowerLimitSetting(
            value=8.0, lower=6.0, upper=16.0
        ),
    )


def _user() -> UserSettings:
    return UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=16.0, lower=6.0, upper=32.0
        ),
        minimum_charging_current=UpperLowerLimitSetting(
            value=6.0, lower=6.0, upper=16.0
        ),
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
#
# The cloud PUT contract is sparse: the app sends only the keys the current
# screen changed, as bare serializer-native values. Every write test below
# therefore asserts the *exact* body the entity produces, not merely that the
# changed field is present. See ``SetUserSettings$$serializer.java`` and
# ``SetSolarSettings$$serializer.java``.


@pytest.mark.parametrize(
    "cls,setter,other_setter,update_cls,value,expected_body",
    [
        (
            RatioMaximumChargingCurrentNumber,
            "set_user_settings",
            "set_solar_settings",
            UserSettingsUpdate,
            20.0,
            {"maximumChargingCurrent": 20},
        ),
        (
            RatioMinimumChargingCurrentNumber,
            "set_user_settings",
            "set_solar_settings",
            UserSettingsUpdate,
            10.0,
            {"minimumChargingCurrent": 10},
        ),
        (
            RatioPureSolarStartingCurrentNumber,
            "set_solar_settings",
            "set_user_settings",
            SolarSettingsUpdate,
            12.0,
            {"pureSolarStartingCurrent": 12},
        ),
        (
            RatioSmartSolarStartingCurrentNumber,
            "set_solar_settings",
            "set_user_settings",
            SolarSettingsUpdate,
            14.0,
            {"smartSolarStartingCurrent": 14},
        ),
        (
            RatioSunOnDelayMinutesNumber,
            "set_solar_settings",
            "set_user_settings",
            SolarSettingsUpdate,
            7.0,
            {"sunOnDelayMinutes": 7},
        ),
        (
            RatioSunOffDelayMinutesNumber,
            "set_solar_settings",
            "set_user_settings",
            SolarSettingsUpdate,
            9.0,
            {"sunOffDelayMinutes": 9},
        ),
    ],
)
@pytest.mark.asyncio
async def test_write_sends_only_the_changed_key(
    cls, setter, other_setter, update_cls, value, expected_body
) -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    setattr(client, setter, AsyncMock())
    setattr(client, other_setter, AsyncMock())

    entity = cls(coord, client, SERIAL)
    await entity.async_set_native_value(value)

    mock = getattr(client, setter)
    mock.assert_awaited_once()
    args, _ = mock.call_args
    assert args[0] == SERIAL
    update = args[1]
    assert isinstance(update, update_cls)
    assert update.to_dict() == expected_body
    getattr(client, other_setter).assert_not_called()


@pytest.mark.asyncio
async def test_write_emits_json_int_not_float() -> None:
    """The serializer types these fields ``Int?``; a float is a wire violation."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(16.0)

    body = client.set_user_settings.call_args[0][1].to_dict()
    assert body == {"maximumChargingCurrent": 16}
    assert type(body["maximumChargingCurrent"]) is int


@pytest.mark.asyncio
async def test_request_command_used_for_writes() -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_solar_settings = AsyncMock()

    entity = RatioPureSolarStartingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(10.0)

    coord.request_command.assert_awaited_once()


# ---- Input validation ----


@pytest.mark.parametrize("value", [6.0, 32.0, 6, 32])
@pytest.mark.asyncio
async def test_values_at_the_bounds_are_accepted(value) -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(value)

    body = client.set_user_settings.call_args[0][1].to_dict()
    assert body == {"maximumChargingCurrent": int(value)}


@pytest.mark.parametrize("value", [5.0, 33.0, 100.0])
@pytest.mark.asyncio
async def test_values_outside_the_bounds_are_rejected(value) -> None:
    """Out-of-range values are refused, never silently clamped."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(value)

    client.set_user_settings.assert_not_called()
    coord.request_command.assert_not_called()


@pytest.mark.parametrize(
    "value", [6.5, 15.5, float("nan"), float("inf"), float("-inf")]
)
@pytest.mark.asyncio
async def test_non_integral_and_non_finite_values_are_rejected(value) -> None:
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(value)

    client.set_user_settings.assert_not_called()


@pytest.mark.parametrize("value", [6.5, float("nan"), float("inf"), float("-inf")])
@pytest.mark.asyncio
async def test_validation_does_not_depend_on_the_cached_settings(value) -> None:
    """With an empty coordinator cache a float used to reach the wire.

    The integrality/finiteness rule must be unconditional, otherwise a
    ``6.5`` silently violates the ``Int?`` serializer contract.
    """
    coord = _make_coordinator(None, None)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(value)

    client.set_user_settings.assert_not_called()
    coord.request_command.assert_not_called()


@pytest.mark.asyncio
async def test_write_works_with_an_empty_cache() -> None:
    """A valid value still writes when the coordinator has no settings yet."""
    coord = _make_coordinator(None, None)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(16.0)

    update = client.set_user_settings.call_args[0][1]
    assert isinstance(update, UserSettingsUpdate)
    assert update.to_dict() == {"maximumChargingCurrent": 16}
