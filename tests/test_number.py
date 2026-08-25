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


# ---- Fail-closed range validation ----
#
# ``_default_min``/``_default_max`` are display scaffolding for the frontend
# slider, not charger limits: the reference charger reports ``upperLimit`` 16
# for the solar starting currents while the class constant says 32. A write
# validated against those constants can therefore be accepted here and
# rejected (or silently clamped) by the cloud. The write path must refuse
# whenever the real bounds are unknown.


@pytest.mark.parametrize("value", [6.0, 16.0, 24.0])
@pytest.mark.asyncio
async def test_empty_cache_rejects_the_write(value) -> None:
    """With no cached settings the charger's bounds are unknown: fail closed."""
    coord = _make_coordinator(None, None)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(value)

    assert err.value.translation_key == "number_bounds_unknown"
    client.set_user_settings.assert_not_called()
    coord.request_command.assert_not_called()


@pytest.mark.asyncio
async def test_empty_cache_rejects_a_value_the_fallback_would_have_accepted() -> None:
    """24 A sits inside the fictional 6-32 fallback but above the real 16 A.

    This is the exact hole the fallback opened: an integral, in-``_default_max``
    value that the charger would never accept.
    """
    coord = _make_coordinator(None, None)
    client = MagicMock()
    client.set_solar_settings = AsyncMock()

    entity = RatioPureSolarStartingCurrentNumber(coord, client, SERIAL)
    assert entity._default_max == 32.0  # the value 24 would have passed
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(24.0)

    assert err.value.translation_key == "number_bounds_unknown"
    client.set_solar_settings.assert_not_called()
    coord.request_command.assert_not_called()


@pytest.mark.asyncio
async def test_populated_cache_rejects_the_same_value_as_out_of_range() -> None:
    """Same 24 A, real bounds 6-16 known: rejected, but as a range error."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_solar_settings = AsyncMock()

    entity = RatioPureSolarStartingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(24.0)

    assert err.value.translation_key == "number_value_out_of_range"
    client.set_solar_settings.assert_not_called()


@pytest.mark.parametrize(
    "lower,upper",
    [(None, 32.0), (6.0, None), (None, None)],
)
@pytest.mark.asyncio
async def test_a_missing_bound_rejects_the_write(lower, upper) -> None:
    """A half-populated descriptor is still "bounds unknown"."""
    user = UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=16.0, lower=lower, upper=upper
        ),
    )
    coord = _make_coordinator(None, user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(16.0)

    assert err.value.translation_key == "number_bounds_unknown"
    client.set_user_settings.assert_not_called()


@pytest.mark.asyncio
async def test_finiteness_and_integrality_still_precede_the_bounds_check() -> None:
    """An empty cache must not mask the unconditional Int? checks."""
    coord = _make_coordinator(None, None)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(6.5)
    assert err.value.translation_key == "number_value_not_integer"

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(float("nan"))
    assert err.value.translation_key == "number_value_not_finite"

    client.set_user_settings.assert_not_called()


def test_display_bounds_keep_the_class_fallbacks() -> None:
    """The frontend slider still needs two floats when the cache is empty.

    ``native_min_value``/``native_max_value`` are typed ``float`` by HA and
    cannot express "unknown"; the entity is unavailable in this state anyway,
    so the fallbacks stay for rendering only. The write path does not use them.
    """
    coord = _make_coordinator(None, None)
    entity = RatioPureSolarStartingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity.available is False
    assert entity.native_min_value == 6.0
    assert entity.native_max_value == 32.0
