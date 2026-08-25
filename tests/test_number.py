"""Tests for Ratio number entities."""

from __future__ import annotations

import math
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import (
    ChargerOverview,
    SolarSettings,
    SolarSettingsUpdate,
    UserSettings,
    UserSettingsUpdate,
)
from aioratio.models.settings import UpperLowerLimitSetting
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.ratio.const import DOMAIN
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
    # Differs from the cached 16.0 so the no-op suppression (#66) does not
    # swallow the write; the point of the test is the wire type, not the value.
    await entity.async_set_native_value(20.0)

    body = client.set_user_settings.call_args[0][1].to_dict()
    assert body == {"maximumChargingCurrent": 20}
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


# ---- Malformed bounds (Finding 2) ----
#
# ``aioratio``'s ``_as_float`` runs the raw JSON through ``float()``, and
# ``float("NaN")``/``float("Infinity")`` both succeed. A NaN upper bound is the
# dangerous one: every comparison against NaN is ``False``, so
# ``as_int < minimum or as_int > maximum`` is ``False`` for *any* value and the
# range check waves the write straight through. Reversed bounds are the
# opposite failure — they reject everything, but as a confusing range error
# quoting limits that cannot both be true.

_NAN = float("nan")
_INF = float("inf")


def _user_with_max_bounds(lower: float | None, upper: float | None) -> UserSettings:
    return UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=16.0, lower=lower, upper=upper
        ),
    )


@pytest.mark.parametrize(
    "lower,upper,why",
    [
        (_NAN, 32.0, "NaN lower"),
        (6.0, _NAN, "NaN upper"),
        (_NAN, _NAN, "both NaN"),
        (6.0, _INF, "+inf upper"),
        (-_INF, 32.0, "-inf lower"),
        (-_INF, _INF, "infinite both ways"),
        (32.0, 6.0, "reversed"),
    ],
)
def test_malformed_bounds_are_not_authoritative(lower, upper, why) -> None:
    """``_bounds()`` must reject anything that cannot order a real range."""
    coord = _make_coordinator(None, _user_with_max_bounds(lower, upper))
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity._bounds() is None, why
    assert entity.available is False, why


@pytest.mark.parametrize(
    "lower,upper",
    [(_NAN, 32.0), (6.0, _NAN), (_NAN, _NAN), (6.0, _INF), (-_INF, 32.0), (32.0, 6.0)],
)
@pytest.mark.asyncio
async def test_malformed_bounds_reject_the_write(lower, upper) -> None:
    coord = _make_coordinator(None, _user_with_max_bounds(lower, upper))
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(16.0)

    assert err.value.translation_key == "number_bounds_unknown"
    client.set_user_settings.assert_not_called()
    coord.request_command.assert_not_called()


@pytest.mark.parametrize("value", [1000.0, -1000.0, 0.0])
@pytest.mark.asyncio
async def test_a_nan_upper_bound_does_not_wave_every_value_through(value) -> None:
    """The hole: ``x > nan`` is ``False``, so the range check accepted anything."""
    coord = _make_coordinator(None, _user_with_max_bounds(6.0, _NAN))
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(value)

    assert err.value.translation_key == "number_bounds_unknown"
    client.set_user_settings.assert_not_called()


@pytest.mark.asyncio
async def test_equal_bounds_are_a_valid_single_legal_value() -> None:
    """``lower == upper`` is a charger pinned to one value, not a malformation."""
    coord = _make_coordinator(None, _user_with_max_bounds(10.0, 10.0))
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    assert entity._bounds() == (10.0, 10.0)
    assert entity.available is True

    await entity.async_set_native_value(10.0)
    assert client.set_user_settings.call_args[0][1].to_dict() == {
        "maximumChargingCurrent": 10
    }

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(11.0)
    assert err.value.translation_key == "number_value_out_of_range"


# ---- Availability follows "bounds known" (Finding 1) ----


def test_unavailable_when_the_descriptor_is_absent() -> None:
    """The settings document exists but this entity's field is missing."""
    coord = _make_coordinator(SolarSettings(), UserSettings())
    client = MagicMock()

    for cls in (
        RatioSunOnDelayMinutesNumber,
        RatioSunOffDelayMinutesNumber,
        RatioPureSolarStartingCurrentNumber,
        RatioSmartSolarStartingCurrentNumber,
        RatioMaximumChargingCurrentNumber,
        RatioMinimumChargingCurrentNumber,
    ):
        entity = cls(coord, client, SERIAL)
        assert entity._settings() is not None
        assert entity._bounds() is None
        assert entity.available is False


@pytest.mark.parametrize("lower,upper", [(None, 32.0), (6.0, None), (None, None)])
def test_unavailable_when_a_bound_is_missing(lower, upper) -> None:
    coord = _make_coordinator(None, _user_with_max_bounds(lower, upper))
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity.available is False


def test_available_when_both_bounds_are_known() -> None:
    coord = _make_coordinator(_solar(), _user())
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity.available is True


def test_coordinator_failure_still_wins_over_known_bounds() -> None:
    """The new clause composes with ``CoordinatorEntity.available``."""
    coord = _make_coordinator(_solar(), _user())
    coord.last_update_success = False
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity._bounds() == (6.0, 32.0)
    assert entity.available is False


def test_wire_safety_survives_independently_of_availability() -> None:
    """``_validate`` must keep failing closed even if ``available`` changed."""
    coord = _make_coordinator(None, None)
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    with pytest.raises(HomeAssistantError) as err:
        entity._validate(16.0)
    assert err.value.translation_key == "number_bounds_unknown"


# ---- End-to-end through ``number.set_value`` ----
#
# The point of the availability rule is what a *user* hits, and the user goes
# through HA's service layer. That layer range-checks and clamps against
# ``native_min_value``/``native_max_value`` — the display fallbacks — before
# ``_validate()`` ever runs, so while a bounds-unknown entity stayed
# "available" the error depended on which side of the fictional fallback the
# requested value fell on: ``number.out_of_range`` outside it,
# ``ratio.number_bounds_unknown`` inside it. Unavailable entities are filtered
# out of entity service calls entirely, so both now behave identically.

_SERVICE_ENTITY_ID = f"number.ratio_{SERIAL.lower()}_maximum_charging_current"


def _overview(serial: str = SERIAL) -> ChargerOverview:
    return ChargerOverview.from_dict({"serialNumber": serial})


def _push(coordinator, user: UserSettings | None) -> None:
    coordinator.async_set_updated_data(
        RatioData(
            chargers={SERIAL: _overview()},
            user_settings={SERIAL: user} if user is not None else {},
        )
    )


@pytest.mark.asyncio
async def test_service_call_writes_when_bounds_are_known(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Positive control: the happy path still goes all the way to the client."""
    coordinator = setup_integration.runtime_data.coordinator
    client = mock_ratio_client.return_value

    _push(coordinator, _user())
    await hass.async_block_till_done()

    assert hass.states.get(_SERVICE_ENTITY_ID).state == "16.0"

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _SERVICE_ENTITY_ID, "value": 20},
        blocking=True,
    )

    client.set_user_settings.assert_awaited_once()
    assert client.set_user_settings.call_args[0][1].to_dict() == {
        "maximumChargingCurrent": 20
    }


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(UserSettings(), id="descriptor-absent"),
        pytest.param(_user_with_max_bounds(6.0, None), id="upper-missing"),
        pytest.param(_user_with_max_bounds(None, 32.0), id="lower-missing"),
        pytest.param(_user_with_max_bounds(6.0, _NAN), id="nan-upper"),
        pytest.param(_user_with_max_bounds(32.0, 6.0), id="reversed"),
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(16, id="inside-the-display-fallback"),
        pytest.param(99, id="outside-the-display-fallback"),
    ],
)
@pytest.mark.asyncio
async def test_service_call_is_a_no_op_when_bounds_are_unknown(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
    user: UserSettings,
    value: int,
) -> None:
    """Same outcome either side of the fallback: unavailable, nothing written."""
    coordinator = setup_integration.runtime_data.coordinator
    client = mock_ratio_client.return_value

    _push(coordinator, user)
    await hass.async_block_till_done()

    state = hass.states.get(_SERVICE_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    # HA keeps the capability attributes on an unavailable state, so display
    # bounds are still rendered — the display/write split working as designed.
    # They must at least stay JSON-representable floats.
    assert math.isfinite(state.attributes["min"])
    assert math.isfinite(state.attributes["max"])

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _SERVICE_ENTITY_ID, "value": value},
        blocking=True,
    )

    client.set_user_settings.assert_not_called()


@pytest.mark.asyncio
async def test_service_call_recovers_when_the_bounds_arrive(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Bounds-unknown is not sticky: the next refresh restores the entity."""
    coordinator = setup_integration.runtime_data.coordinator
    client = mock_ratio_client.return_value

    _push(coordinator, UserSettings())
    await hass.async_block_till_done()
    assert hass.states.get(_SERVICE_ENTITY_ID).state == STATE_UNAVAILABLE

    _push(coordinator, _user())
    await hass.async_block_till_done()
    assert hass.states.get(_SERVICE_ENTITY_ID).state == "16.0"

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _SERVICE_ENTITY_ID, "value": 12},
        blocking=True,
    )
    client.set_user_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_entity_is_available_from_its_first_state_write(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """No startup flap: entities are only added once a refresh has landed.

    ``async_setup_entry`` adds nothing while ``coordinator.data`` is ``None``,
    and the coordinator gathers chargers and their settings in the same
    refresh — so an entity's very first written state already has bounds. It
    never passes through an ``unavailable`` state on the way up.
    """
    coordinator = setup_integration.runtime_data.coordinator

    assert hass.states.get(_SERVICE_ENTITY_ID) is None

    states: list[str] = []

    @callback
    def _record(event) -> None:
        new = event.data["new_state"]
        if new is not None:
            states.append(new.state)

    unsub = async_track_state_change_event(hass, [_SERVICE_ENTITY_ID], _record)
    _push(coordinator, _user())
    await hass.async_block_till_done()
    unsub()

    assert states == ["16.0"]


@pytest.mark.parametrize("lower,upper", [(_NAN, 32.0), (6.0, _NAN), (-_INF, _INF)])
def test_non_finite_bounds_never_reach_the_display_attributes(lower, upper) -> None:
    """``min``/``max`` land in the state machine; NaN is not valid JSON."""
    coord = _make_coordinator(None, _user_with_max_bounds(lower, upper))
    entity = RatioMaximumChargingCurrentNumber(coord, MagicMock(), SERIAL)

    assert entity.native_min_value == 6.0
    assert entity.native_max_value == 32.0


# ---------------------------------------------------------------------------
# Post-write refresh timing (issue #69)
# ---------------------------------------------------------------------------


def _limit(value: float) -> UpperLowerLimitSetting:
    return UpperLowerLimitSetting(value=value, lower=6.0, upper=32.0)


@pytest.mark.asyncio
async def test_post_write_refresh_lands_after_cloud_propagation(
    hass: HomeAssistant,
    mock_config_entry,
    mock_ratio_client: MagicMock,
) -> None:
    """A write's refresh must read the cloud *after* it has propagated.

    The Ratio cloud takes ~3-6 s to make a PUT visible to a subsequent GET,
    and it cascades ``maximumChargingCurrent`` onto
    ``smartSolarStartingCurrent`` server-side (issue #69). A refresh issued
    the instant the PUT returns therefore reads pre-write values for *both*
    entities, and nothing reads again until the 60 s poll.

    ``freezegun`` cannot be used here — freezing the clock across
    ``async_setup`` breaks ``mashumaro``'s code generation in the ``usb``
    dependency — so wall-clock is modelled with an explicit counter and the
    debouncer's timer is fired by hand.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    client = mock_ratio_client.return_value
    client.chargers_overview = AsyncMock(
        return_value=[ChargerOverview.from_dict({"serialNumber": SERIAL})]
    )

    # Modelled wall-clock, in seconds, advanced explicitly by the test.
    clock = {"now": 0.0}
    propagated_at: dict[str, float | None] = {"at": None}
    _PROPAGATION_S = 6.0

    async def _set_user_settings(*_args, **_kwargs) -> None:
        propagated_at["at"] = clock["now"] + _PROPAGATION_S

    def _current(pre: float, post: float) -> float:
        at = propagated_at["at"]
        return post if at is not None and clock["now"] >= at else pre

    async def _user_settings(_serial: str) -> UserSettings:
        return UserSettings(
            maximum_charging_current=_limit(_current(16.0, 15.0)),
            minimum_charging_current=_limit(6.0),
        )

    async def _solar_settings(_serial: str) -> SolarSettings:
        # The server-side cascade lowers the smart-solar starting current too.
        return SolarSettings(
            sun_on_delay_minutes=UpperLowerLimitSetting(
                value=2.0, lower=0.0, upper=10.0
            ),
            sun_off_delay_minutes=UpperLowerLimitSetting(
                value=3.0, lower=0.0, upper=15.0
            ),
            pure_solar_starting_current=_limit(6.0),
            smart_solar_starting_current=_limit(_current(16.0, 15.0)),
        )

    client.set_user_settings = AsyncMock(side_effect=_set_user_settings)
    client.user_settings = AsyncMock(side_effect=_user_settings)
    client.solar_settings = AsyncMock(side_effect=_solar_settings)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    max_current = registry.async_get_entity_id(
        "number", DOMAIN, f"{SERIAL}_maximum_charging_current"
    )
    smart_solar = registry.async_get_entity_id(
        "number", DOMAIN, f"{SERIAL}_smart_solar_starting_current"
    )
    assert max_current is not None and smart_solar is not None
    assert hass.states.get(max_current).state == "16.0"
    assert hass.states.get(smart_solar).state == "16.0"

    solar_reads_before = client.solar_settings.await_count

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": max_current, "value": 15},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Nothing correct can be known yet — the cloud has not propagated the PUT.
    assert hass.states.get(smart_solar).state == "16.0"

    # Wall-clock moves past the propagation window; the post-write refresh
    # is due at POST_WRITE_SETTLE_SECONDS.
    clock["now"] += 15.0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
    await hass.async_block_till_done()

    assert hass.states.get(max_current).state == "15.0"
    assert hass.states.get(smart_solar).state == "15.0"
    assert client.solar_settings.await_count == solar_reads_before + 1

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# No-op write suppression (issue #66)
# ---------------------------------------------------------------------------
#
# A ``number.set_value`` asking for the value the entity already holds must not
# reach the cloud. A same-value PUT was verified inert on the live charger
# (`maximumChargingCurrent` re-written to the value it already held did not
# re-trigger the `smartSolarStartingCurrent` cascade, and moved nothing), so
# suppression applies to every number entity with no exclusions.
#
# Suppression is compared against the *validated int*, and runs strictly after
# ``_validate()`` so an invalid request is still reported as invalid rather
# than silently swallowed.


@pytest.mark.parametrize(
    "cls,setter,cached",
    [
        (RatioSunOnDelayMinutesNumber, "set_solar_settings", 2.0),
        (RatioSunOffDelayMinutesNumber, "set_solar_settings", 3.0),
        (RatioPureSolarStartingCurrentNumber, "set_solar_settings", 6.0),
        (RatioSmartSolarStartingCurrentNumber, "set_solar_settings", 8.0),
        (RatioMaximumChargingCurrentNumber, "set_user_settings", 16.0),
        (RatioMinimumChargingCurrentNumber, "set_user_settings", 6.0),
    ],
)
@pytest.mark.asyncio
async def test_writing_the_cached_value_suppresses_the_put(cls, setter, cached) -> None:
    """The value the entity already holds is not re-sent."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    setattr(client, setter, AsyncMock())

    entity = cls(coord, client, SERIAL)
    assert entity.native_value == cached

    await entity.async_set_native_value(cached)

    getattr(client, setter).assert_not_awaited()
    coord.request_command.assert_not_called()


@pytest.mark.parametrize(
    "cls,setter,cached",
    [
        (RatioSunOnDelayMinutesNumber, "set_solar_settings", 2.0),
        (RatioSunOffDelayMinutesNumber, "set_solar_settings", 3.0),
        (RatioPureSolarStartingCurrentNumber, "set_solar_settings", 6.0),
        (RatioSmartSolarStartingCurrentNumber, "set_solar_settings", 8.0),
        (RatioMaximumChargingCurrentNumber, "set_user_settings", 16.0),
        (RatioMinimumChargingCurrentNumber, "set_user_settings", 6.0),
    ],
)
@pytest.mark.asyncio
async def test_writing_a_different_value_still_sends(cls, setter, cached) -> None:
    """Guard against over-suppression: a real change must still go out."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    setattr(client, setter, AsyncMock())

    entity = cls(coord, client, SERIAL)
    await entity.async_set_native_value(cached + 1.0)

    getattr(client, setter).assert_awaited_once()
    coord.request_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_identical_writes_of_a_new_value_send_one_put() -> None:
    """Issue #66's headline case.

    The cloud takes 3-6 s to make a PUT readable and the confirming refresh
    waits 10 s, so a controller re-asserting its target faster than that used
    to PUT on every tick while the cache still held the old value. The
    pending target closes that window.
    """
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    assert entity.native_value == 16.0

    await entity.async_set_native_value(20.0)
    await entity.async_set_native_value(20.0)

    # The cache still says 16.0 — only the pending target can suppress this.
    assert entity.native_value == 16.0
    client.set_user_settings.assert_awaited_once()
    assert coord.request_command.await_count == 1


@pytest.mark.asyncio
async def test_a_coordinator_update_clears_the_pending_target() -> None:
    """Suppression is bounded to one refresh cycle and fails toward sending.

    A write that never landed server-side must be retried rather than
    suppressed forever, so the pending target is dropped on the next
    coordinator update regardless of what that update reported.
    """
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(20.0)
    assert client.set_user_settings.await_count == 1

    # A refresh landed; it still reports the old value (the PUT did not stick).
    entity.async_write_ha_state = MagicMock()
    entity._handle_coordinator_update()

    await entity.async_set_native_value(20.0)
    assert client.set_user_settings.await_count == 2


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_pending_target() -> None:
    """Only a *successful* PUT arms suppression."""
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock(side_effect=HomeAssistantError("boom"))

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(20.0)

    client.set_user_settings = AsyncMock()
    await entity.async_set_native_value(20.0)
    client.set_user_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_pending_target_takes_precedence_over_the_stale_cache() -> None:
    """Once a write is in flight the cache is known-stale and must not suppress.

    Cache 16, write 20, then write 16 back: the charger is at 20, so the 16
    is a real change and has to go out even though it equals the cache.
    """
    coord = _make_coordinator(_solar(), _user())
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(20.0)
    await entity.async_set_native_value(16.0)

    assert client.set_user_settings.await_count == 2
    assert client.set_user_settings.call_args[0][1].to_dict() == {
        "maximumChargingCurrent": 16
    }


@pytest.mark.asyncio
async def test_a_nan_cached_value_still_sends() -> None:
    """``_bounds()`` checks the limits' finiteness, never the value's.

    ``aioratio`` parses the value with a bare ``float()``, so an *available*
    entity can hold ``NaN``. Every comparison against it is ``False``, but
    make the finiteness check explicit rather than leaning on that.
    """
    user = UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=_NAN, lower=6.0, upper=32.0
        ),
    )
    coord = _make_coordinator(None, user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    assert entity.available is True
    assert math.isnan(entity.native_value)

    await entity.async_set_native_value(16.0)
    client.set_user_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_non_integral_cached_value_still_sends() -> None:
    """``6`` is not ``6.5``: writing the integral neighbour is a real change."""
    user = UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=6.5, lower=6.0, upper=32.0
        ),
    )
    coord = _make_coordinator(None, user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(6.0)

    assert client.set_user_settings.call_args[0][1].to_dict() == {
        "maximumChargingCurrent": 6
    }


@pytest.mark.asyncio
async def test_a_missing_cached_value_still_sends() -> None:
    """Bounds present, ``value`` absent: nothing to compare against, so send."""
    user = UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=None, lower=6.0, upper=32.0
        ),
    )
    coord = _make_coordinator(None, user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    assert entity.available is True
    assert entity.native_value is None

    await entity.async_set_native_value(16.0)
    client.set_user_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_validation_precedes_suppression_for_an_out_of_range_value() -> None:
    """An invalid request is reported as invalid, not silently swallowed."""
    user = UserSettings(
        maximum_charging_current=UpperLowerLimitSetting(
            value=20.0, lower=6.0, upper=16.0
        ),
    )
    coord = _make_coordinator(None, user)
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    assert entity.native_value == 20.0
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(20.0)

    assert err.value.translation_key == "number_value_out_of_range"
    client.set_user_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_precedes_suppression_for_a_non_integral_value() -> None:
    coord = _make_coordinator(
        None,
        UserSettings(
            maximum_charging_current=UpperLowerLimitSetting(
                value=6.5, lower=6.0, upper=32.0
            ),
        ),
    )
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_native_value(6.5)

    assert err.value.translation_key == "number_value_not_integer"
    client.set_user_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_call_suppresses_a_write_of_the_current_value(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """End to end: nothing is written and no settle refresh is armed."""
    coordinator = setup_integration.runtime_data.coordinator
    client = mock_ratio_client.return_value

    _push(coordinator, _user())
    await hass.async_block_till_done()
    assert hass.states.get(_SERVICE_ENTITY_ID).state == "16.0"

    coordinator._schedule_settle_refresh = MagicMock()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _SERVICE_ENTITY_ID, "value": 16},
        blocking=True,
    )

    client.set_user_settings.assert_not_called()
    coordinator._schedule_settle_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_service_call_repeating_a_new_value_writes_once(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """End to end for #66: a controller re-asserting its target PUTs once."""
    coordinator = setup_integration.runtime_data.coordinator
    client = mock_ratio_client.return_value

    _push(coordinator, _user())
    await hass.async_block_till_done()

    for _ in range(3):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": _SERVICE_ENTITY_ID, "value": 20},
            blocking=True,
        )

    client.set_user_settings.assert_awaited_once()

    # The next refresh clears the pending target, so a still-unapplied write
    # is retried rather than suppressed forever.
    _push(coordinator, _user())
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _SERVICE_ENTITY_ID, "value": 20},
        blocking=True,
    )
    assert client.set_user_settings.await_count == 2
