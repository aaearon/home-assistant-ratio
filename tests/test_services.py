"""Tests for Ratio service helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from aioratio.models import Vehicle
from aioratio.models.history import Session, SessionHistoryPage, TimeData

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ratio.const import (
    ATTR_SLOTS,
    DOMAIN,
    SERVICE_ADD_VEHICLE,
    SERVICE_IMPORT_SESSION_HISTORY,
    SERVICE_REMOVE_VEHICLE,
)
from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.services import (
    SET_SCHEDULE_SCHEMA,
    _resolve_serials,
    async_setup_services,
    async_unload_services,
)


def _make_device(config_entries: set[str], serial: str = "SN001") -> MagicMock:
    dev = MagicMock()
    dev.config_entries = config_entries
    dev.identifiers = {(DOMAIN, serial)}
    return dev


@pytest.mark.asyncio
async def test_resolve_serials_picks_active_entry(hass: HomeAssistant) -> None:
    """_resolve_serials should skip stale entries and pick the active one."""
    active_id = "entry_active"
    stale_id = "entry_stale"

    # Only active_id is present in hass.data[DOMAIN]
    hass.data[DOMAIN] = {active_id: {"client": MagicMock(), "coordinator": MagicMock()}}

    device = _make_device(config_entries={stale_id, active_id}, serial="SN001")

    with patch(
        "custom_components.ratio.services.dr.async_get"
    ) as mock_dr:
        mock_dr.return_value.async_get.return_value = device

        call = MagicMock()
        call.data = {"device_id": "dev_123"}

        pairs = _resolve_serials(hass, call)

    assert len(pairs) == 1
    entry_id, serial = pairs[0]
    assert entry_id == active_id
    assert serial == "SN001"


def test_schedule_schema_rejects_non_dict_slots() -> None:
    """Passing non-dict items in slots should raise vol.Invalid."""
    with pytest.raises(vol.Invalid):
        SET_SCHEDULE_SCHEMA({"device_id": "x", "slots": ["not-a-dict"]})


@pytest.mark.asyncio
async def test_add_vehicle_returns_vehicle_id_in_response(
    hass: HomeAssistant,
) -> None:
    """ratio.add_vehicle should call client.add_vehicle and return the new id."""
    client = MagicMock()
    new_vehicle = Vehicle(vehicle_id="v-new", vehicle_name="Tesla")
    client.add_vehicle = AsyncMock(return_value=new_vehicle)

    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()

    hass.data[DOMAIN] = {"entry1": {"client": client, "coordinator": coordinator}}

    await async_setup_services(hass)
    try:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_VEHICLE,
            {"vehicle_name": "Tesla", "license_plate": "AB-12-CD"},
            blocking=True,
            return_response=True,
        )
    finally:
        await async_unload_services(hass)

    assert response == {"vehicle_id": "v-new"}
    # Ensure client got a Vehicle with the right name + plate.
    sent: Vehicle = client.add_vehicle.await_args.args[0]
    assert sent.vehicle_name == "Tesla"
    assert sent.license_plate == "AB-12-CD"
    coordinator.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_remove_vehicle_clears_stale_preferred_entries(
    hass: HomeAssistant,
) -> None:
    """ratio.remove_vehicle should drop preferred_vehicle entries pointing at the removed id."""
    client = MagicMock()
    client.remove_vehicle = AsyncMock()

    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_save_preferences = AsyncMock()
    coordinator.preferred_vehicle = {
        "SN001": "v-doomed",
        "SN002": "v-keep",
        "SN003": "v-doomed",
    }

    hass.data[DOMAIN] = {"entry1": {"client": client, "coordinator": coordinator}}

    await async_setup_services(hass)
    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REMOVE_VEHICLE,
            {"vehicle_id": "v-doomed"},
            blocking=True,
        )
    finally:
        await async_unload_services(hass)

    client.remove_vehicle.assert_awaited_once_with("v-doomed")
    assert coordinator.preferred_vehicle == {"SN002": "v-keep"}
    coordinator.async_save_preferences.assert_awaited()


@pytest.mark.asyncio
async def test_import_session_history_imports_for_window(
    hass: HomeAssistant,
) -> None:
    """ratio.import_session_history should fetch the window and import statistics."""
    serial = "SN-IMP"
    client = MagicMock()
    s1 = Session(
        session_id="hid-1",
        charger_serial_number=serial,
        total_charging_energy=2000,
        begin=TimeData(time=1_700_000_000),
        end=TimeData(time=1_700_000_600),
    )
    s2 = Session(
        session_id="hid-2",
        charger_serial_number=serial,
        total_charging_energy=3000,
        begin=TimeData(time=1_700_010_000),
        end=TimeData(time=1_700_010_600),
    )
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1, s2], next_token=None)
    )

    main = MagicMock()
    main.data = RatioData(chargers={serial: MagicMock()})

    history = MagicMock()
    history.async_import_window = AsyncMock(return_value={serial: 2})

    hass.data[DOMAIN] = {
        "entry1": {
            "client": client,
            "coordinator": main,
            "history_coordinator": history,
        }
    }

    begin_dt = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end_dt = datetime(2023, 12, 1, tzinfo=timezone.utc)

    await async_setup_services(hass)
    try:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_SESSION_HISTORY,
            {"begin_time": begin_dt.isoformat(), "end_time": end_dt.isoformat()},
            blocking=True,
            return_response=True,
        )
    finally:
        await async_unload_services(hass)

    history.async_import_window.assert_awaited_once()
    call_kwargs = history.async_import_window.await_args.kwargs
    call_args = history.async_import_window.await_args.args
    # Accept either positional or keyword form.
    begin_arg = call_kwargs.get("begin_time", call_args[0] if call_args else None)
    end_arg = call_kwargs.get("end_time", call_args[1] if len(call_args) > 1 else None)
    assert int(begin_arg.timestamp()) == int(begin_dt.timestamp())
    assert int(end_arg.timestamp()) == int(end_dt.timestamp())
    assert response == {"imported": {serial: 2}}


@pytest.mark.asyncio
async def test_history_coordinator_async_import_window(
    hass: HomeAssistant,
) -> None:
    """RatioHistoryCoordinator.async_import_window fetches and imports for each serial."""
    from unittest.mock import patch as _patch

    from aioratio.models import ChargerOverview

    from homeassistant.config_entries import ConfigEntry

    from custom_components.ratio.coordinator import RatioHistoryCoordinator

    serial = "ABC"
    client = MagicMock()
    s1 = Session(
        session_id="x-1",
        charger_serial_number=serial,
        total_charging_energy=1000,
        begin=TimeData(time=1_700_000_000),
        end=TimeData(time=1_700_000_600),
    )
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    entry = MagicMock(spec=ConfigEntry, entry_id="ew")
    coord = RatioHistoryCoordinator(hass, client, entry)

    main = MagicMock()
    main.data = RatioData(
        chargers={serial: ChargerOverview.from_dict({"serialNumber": serial})}
    )
    hass.data.setdefault(DOMAIN, {}).setdefault("ew", {})["coordinator"] = main

    async def _fake(hass, ser, sessions, starting_total):
        return float(starting_total) + sum(s.total_charging_energy for s in sessions)

    begin = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end = datetime(2023, 12, 1, tzinfo=timezone.utc)

    with _patch(
        "custom_components.ratio.coordinator.async_import_sessions",
        new=AsyncMock(side_effect=_fake),
    ) as mock_import:
        result = await coord.async_import_window(begin_time=begin, end_time=end)

    assert result == {serial: 1}
    # session_history was called with our begin/end and the right serial.
    call = client.session_history.await_args
    assert call.kwargs["serial_number"] == serial
    assert call.kwargs["begin_time"] == int(begin.timestamp())
    assert call.kwargs["end_time"] == int(end.timestamp())
    mock_import.assert_awaited()
