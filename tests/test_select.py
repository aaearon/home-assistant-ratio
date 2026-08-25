"""Tests for Ratio select entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio.models import ChargerOverview, UserSettings, Vehicle
from aioratio.models.settings import ChargeModeSettings, EnumValue

from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.select import (
    RatioActiveVehicleSelect,
    RatioCableSettingsSelect,
    RatioChargeModeSelect,
    RatioStartModeSelect,
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

    async def _set_preferred(serial: str, vehicle_id: str) -> None:
        coord.preferred_vehicle[serial] = vehicle_id

    coord.async_set_preferred_vehicle = AsyncMock(side_effect=_set_preferred)
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
        charging_mode=ChargeModeSettings(
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
        charging_mode=ChargeModeSettings(
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
    coord.async_set_preferred_vehicle.assert_awaited_once_with(SERIAL, "v2")
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


# ---- Charge mode isChangeAllowed gating ----


def _charge_mode_coord(
    is_change_allowed: bool | None = None,
    *,
    with_settings: bool = True,
) -> MagicMock:
    """Coordinator carrying the charger plus an optional charging-mode setting."""
    user_settings = {}
    if with_settings:
        kwargs = {
            "value": "Smart",
            "allowed_values": ["Smart", "SmartSolar", "PureSolar"],
        }
        if is_change_allowed is not None:
            kwargs["is_change_allowed"] = is_change_allowed
        user_settings[SERIAL] = UserSettings(charging_mode=ChargeModeSettings(**kwargs))
    coord = _make_coordinator(user_settings=user_settings)
    coord.data.chargers = {SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})}
    coord.last_update_success = True
    return coord


def test_charge_mode_unavailable_when_change_not_allowed() -> None:
    """A charger that refuses mode changes must not offer a writable entity.

    ``ChargeModeSettings$$serializer.java``:41-43 declares ``isChangeAllowed``
    required. HA filters unavailable entities out of ``entity_service_call``,
    so the gate is what actually blocks the write — the same treatment
    ``select.cpms``, ``text.charge_point_identifier`` and
    ``switch.ocpp_enabled`` already give their own flags.
    """
    entity = RatioChargeModeSelect(_charge_mode_coord(False), MagicMock(), SERIAL)
    assert entity.available is False


def test_charge_mode_available_when_change_allowed() -> None:
    entity = RatioChargeModeSelect(_charge_mode_coord(True), MagicMock(), SERIAL)
    assert entity.available is True


def test_charge_mode_available_when_flag_absent() -> None:
    """An omitted flag means "assume writable" — never black the entity out."""
    entity = RatioChargeModeSelect(_charge_mode_coord(None), MagicMock(), SERIAL)
    assert entity.available is True


def test_charge_mode_available_when_settings_missing() -> None:
    """No settings document yet is not a statement that the setting is locked."""
    entity = RatioChargeModeSelect(
        _charge_mode_coord(with_settings=False), MagicMock(), SERIAL
    )
    assert entity.available is True


# ---- StartModeSelect unit tests ----


def _enum_coord(
    field: str,
    value: str,
    allowed_values: list[str] | None,
    is_change_allowed: bool | None = None,
    *,
    with_settings: bool = True,
) -> MagicMock:
    """Coordinator carrying the charger plus an optional ``EnumValue`` setting."""
    user_settings = {}
    if with_settings:
        kwargs: dict = {"value": value}
        if allowed_values is not None:
            kwargs["allowed_values"] = allowed_values
        if is_change_allowed is not None:
            kwargs["is_change_allowed"] = is_change_allowed
        user_settings[SERIAL] = UserSettings(**{field: EnumValue(**kwargs)})
    coord = _make_coordinator(user_settings=user_settings)
    coord.data.chargers = {SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})}
    coord.last_update_success = True
    return coord


def _start_mode_coord(
    allowed_values: list[str] | None = None,
    is_change_allowed: bool | None = None,
    *,
    with_settings: bool = True,
) -> MagicMock:
    return _enum_coord(
        "start_mode",
        "Auto",
        ["Manual", "Auto"] if allowed_values is None else allowed_values,
        is_change_allowed,
        with_settings=with_settings,
    )


def test_start_mode_options_from_settings() -> None:
    """Options come from the charger's reported ``allowedValues``, in its order.

    The live charger reports ``[Manual, Auto]`` — the reverse of the
    ``StartMode.java`` enum ordinals — so nothing may re-sort this list.
    """
    entity = RatioStartModeSelect(_start_mode_coord(), MagicMock(), SERIAL)
    assert entity.options == ["Manual", "Auto"]


def test_start_mode_current_option_from_settings() -> None:
    entity = RatioStartModeSelect(_start_mode_coord(), MagicMock(), SERIAL)
    assert entity.current_option == "Auto"


def test_start_mode_options_empty_when_allowed_values_absent() -> None:
    """No hardcoded fallback: an absent list means we offer nothing.

    Enum ordinals are not a safe source for a fallback (the charger reports
    the reverse order), and we have data from exactly one charger.
    """
    entity = RatioStartModeSelect(
        _start_mode_coord(allowed_values=[]), MagicMock(), SERIAL
    )
    assert entity.options == []


def test_start_mode_options_empty_when_settings_missing() -> None:
    entity = RatioStartModeSelect(
        _start_mode_coord(with_settings=False), MagicMock(), SERIAL
    )
    assert entity.options == []
    assert entity.current_option is None


def test_start_mode_options_empty_when_data_is_none() -> None:
    coord = MagicMock()
    coord.data = None
    entity = RatioStartModeSelect(coord, MagicMock(), SERIAL)
    assert entity.options == []
    assert entity.current_option is None


def test_start_mode_unavailable_when_change_not_allowed() -> None:
    entity = RatioStartModeSelect(
        _start_mode_coord(is_change_allowed=False), MagicMock(), SERIAL
    )
    assert entity.available is False


def test_start_mode_available_when_change_allowed() -> None:
    entity = RatioStartModeSelect(
        _start_mode_coord(is_change_allowed=True), MagicMock(), SERIAL
    )
    assert entity.available is True


def test_start_mode_available_when_flag_absent() -> None:
    """An omitted flag means "assume writable" — never black the entity out."""
    entity = RatioStartModeSelect(_start_mode_coord(), MagicMock(), SERIAL)
    assert entity.available is True


def test_start_mode_available_when_settings_missing() -> None:
    """No settings document yet is not a statement that the setting is locked."""
    entity = RatioStartModeSelect(
        _start_mode_coord(with_settings=False), MagicMock(), SERIAL
    )
    assert entity.available is True


@pytest.mark.asyncio
async def test_start_mode_select_option() -> None:
    """Selecting issues a sparse single-key PUT on ``startMode``."""
    coord = _start_mode_coord()
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    async def _passthrough(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    coord.request_command = AsyncMock(side_effect=_passthrough)

    entity = RatioStartModeSelect(coord, client, SERIAL)
    await entity.async_select_option("Manual")

    coord.request_command.assert_awaited_once()
    client.set_user_settings.assert_awaited_once_with(SERIAL, {"startMode": "Manual"})


# ---- CableSettingsSelect unit tests ----


_CABLE_OPTIONS = ["LockWhenCarConnected", "LockAutomatically", "LockAlways"]


def _cable_settings_coord(
    allowed_values: list[str] | None = None,
    is_change_allowed: bool | None = None,
    *,
    with_settings: bool = True,
) -> MagicMock:
    return _enum_coord(
        "cable_settings",
        "LockWhenCarConnected",
        list(_CABLE_OPTIONS) if allowed_values is None else allowed_values,
        is_change_allowed,
        with_settings=with_settings,
    )


def test_cable_settings_options_from_settings() -> None:
    entity = RatioCableSettingsSelect(_cable_settings_coord(), MagicMock(), SERIAL)
    assert entity.options == _CABLE_OPTIONS


def test_cable_settings_current_option_from_settings() -> None:
    entity = RatioCableSettingsSelect(_cable_settings_coord(), MagicMock(), SERIAL)
    assert entity.current_option == "LockWhenCarConnected"


def test_cable_settings_options_empty_when_allowed_values_absent() -> None:
    """No hardcoded fallback — the option semantics are uncharacterised."""
    entity = RatioCableSettingsSelect(
        _cable_settings_coord(allowed_values=[]), MagicMock(), SERIAL
    )
    assert entity.options == []


def test_cable_settings_options_empty_when_settings_missing() -> None:
    entity = RatioCableSettingsSelect(
        _cable_settings_coord(with_settings=False), MagicMock(), SERIAL
    )
    assert entity.options == []
    assert entity.current_option is None


def test_cable_settings_options_empty_when_data_is_none() -> None:
    coord = MagicMock()
    coord.data = None
    entity = RatioCableSettingsSelect(coord, MagicMock(), SERIAL)
    assert entity.options == []
    assert entity.current_option is None


def test_cable_settings_unavailable_when_change_not_allowed() -> None:
    entity = RatioCableSettingsSelect(
        _cable_settings_coord(is_change_allowed=False), MagicMock(), SERIAL
    )
    assert entity.available is False


def test_cable_settings_available_when_change_allowed() -> None:
    entity = RatioCableSettingsSelect(
        _cable_settings_coord(is_change_allowed=True), MagicMock(), SERIAL
    )
    assert entity.available is True


def test_cable_settings_available_when_flag_absent() -> None:
    entity = RatioCableSettingsSelect(_cable_settings_coord(), MagicMock(), SERIAL)
    assert entity.available is True


def test_cable_settings_available_when_settings_missing() -> None:
    entity = RatioCableSettingsSelect(
        _cable_settings_coord(with_settings=False), MagicMock(), SERIAL
    )
    assert entity.available is True


@pytest.mark.asyncio
async def test_cable_settings_select_option() -> None:
    """Selecting issues a sparse single-key PUT on ``cableSettings``."""
    coord = _cable_settings_coord()
    client = MagicMock()
    client.set_user_settings = AsyncMock()

    async def _passthrough(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    coord.request_command = AsyncMock(side_effect=_passthrough)

    entity = RatioCableSettingsSelect(coord, client, SERIAL)
    await entity.async_select_option("LockAlways")

    coord.request_command.assert_awaited_once()
    client.set_user_settings.assert_awaited_once_with(
        SERIAL, {"cableSettings": "LockAlways"}
    )
