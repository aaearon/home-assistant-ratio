"""Tests for Ratio service helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from aioratio.models import Vehicle
from aioratio.models.history import Session, SessionHistoryPage, TimeData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.ratio.const import (
    ATTR_SLOTS,
    DOMAIN,
    SERVICE_ADD_VEHICLE,
    SERVICE_IMPORT_SESSION_HISTORY,
    SERVICE_REMOVE_VEHICLE,
)
from custom_components.ratio.services import (
    SET_SCHEDULE_SCHEMA,
    _resolve_serials,
)


def _make_entry(hass: HomeAssistant, entry_id: str = "entry1") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_resolve_serials_picks_active_entry(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """_resolve_serials should skip stale entries and pick the active one."""
    active_entry = _make_entry(hass, entry_id="entry_active")
    stale_entry = _make_entry(hass, entry_id="entry_stale")

    # Only active_id is present in hass.data[DOMAIN]
    hass.data[DOMAIN] = {
        active_entry.entry_id: {"client": MagicMock(), "coordinator": MagicMock()},
    }

    device = device_registry.async_get_or_create(
        config_entry_id=stale_entry.entry_id,
        identifiers={(DOMAIN, "SN001")},
    )
    device_registry.async_update_device(
        device.id, add_config_entry_id=active_entry.entry_id
    )

    call = MagicMock()
    call.data = {"device_id": device.id}

    pairs = _resolve_serials(hass, call)

    assert len(pairs) == 1
    entry_id, serial = pairs[0]
    assert entry_id == active_entry.entry_id
    assert serial == "SN001"


def test_schedule_schema_rejects_non_dict_slots() -> None:
    """Passing non-dict items in slots should raise vol.Invalid."""
    with pytest.raises(vol.Invalid):
        SET_SCHEDULE_SCHEMA({"device_id": "x", "slots": ["not-a-dict"]})


@pytest.mark.asyncio
async def test_set_schedule_calls_client_directly(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """_handle_set_schedule should pass client.set_charge_schedule to request_command."""
    from aioratio.models import ChargeSchedule

    entry = setup_integration
    client = mock_ratio_client.return_value

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN-SCH")},
    )

    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "device_id": device.id,
            "slots": [{"start": "22:00", "end": "06:00", "days": ["monday"]}],
        },
        blocking=True,
    )

    client.set_charge_schedule.assert_awaited_once()
    args = client.set_charge_schedule.await_args.args
    assert args[0] == "SN-SCH"
    assert isinstance(args[1], ChargeSchedule)


@pytest.mark.asyncio
async def test_add_vehicle_returns_vehicle_id_in_response(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.add_vehicle should call client.add_vehicle and return the new id."""
    entry = setup_integration
    client = mock_ratio_client.return_value
    new_vehicle = Vehicle(vehicle_id="v-new", vehicle_name="Tesla")
    client.add_vehicle = AsyncMock(return_value=new_vehicle)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_VEHICLE,
        {"vehicle_name": "Tesla", "license_plate": "AB-12-CD"},
        blocking=True,
        return_response=True,
    )

    assert response == {"vehicle_id": "v-new"}
    sent: Vehicle = client.add_vehicle.await_args.args[0]
    assert sent.vehicle_name == "Tesla"
    assert sent.license_plate == "AB-12-CD"


@pytest.mark.asyncio
async def test_remove_vehicle_clears_stale_preferred_entries(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.remove_vehicle should drop preferred_vehicle entries pointing at the removed id."""
    entry = setup_integration
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.preferred_vehicle = {
        "SN001": "v-doomed",
        "SN002": "v-keep",
        "SN003": "v-doomed",
    }

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REMOVE_VEHICLE,
        {"vehicle_id": "v-doomed"},
        blocking=True,
    )

    mock_ratio_client.return_value.remove_vehicle.assert_awaited_once_with("v-doomed")
    assert coordinator.preferred_vehicle == {"SN002": "v-keep"}


@pytest.mark.asyncio
async def test_import_session_history_imports_for_window(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.import_session_history should fetch the window and import statistics."""
    entry = setup_integration
    history = hass.data[DOMAIN][entry.entry_id]["history_coordinator"]

    begin_dt = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end_dt = datetime(2023, 12, 1, tzinfo=timezone.utc)

    with patch.object(
        history, "async_import_window", new=AsyncMock(return_value={"SN-IMP": 2})
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_SESSION_HISTORY,
            {"begin_time": begin_dt.isoformat(), "end_time": end_dt.isoformat()},
            blocking=True,
            return_response=True,
        )

    assert response == {"imported": {"SN-IMP": 2}}


@pytest.mark.asyncio
async def test_history_coordinator_async_import_window(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """RatioHistoryCoordinator.async_import_window fetches and imports for each serial."""
    from aioratio.models import ChargerOverview

    entry = setup_integration
    client = mock_ratio_client.return_value
    history = hass.data[DOMAIN][entry.entry_id]["history_coordinator"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    serial = "ABC"
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

    # Populate the main coordinator with a charger so async_import_window
    # knows which serials to iterate.
    from custom_components.ratio.coordinator import RatioData

    coordinator.data = RatioData(
        chargers={serial: ChargerOverview.from_dict({"serialNumber": serial})}
    )

    async def _fake(hass, ser, sessions, starting_total):
        return float(starting_total) + sum(s.total_charging_energy for s in sessions)

    begin = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end = datetime(2023, 12, 1, tzinfo=timezone.utc)

    with patch(
        "custom_components.ratio.coordinator.async_import_sessions",
        new=AsyncMock(side_effect=_fake),
    ) as mock_import:
        result = await history.async_import_window(begin_time=begin, end_time=end)

    assert result == {serial: 1}
    call = client.session_history.await_args
    assert call.kwargs["serial_number"] == serial
    assert call.kwargs["begin_time"] == int(begin.timestamp())
    assert call.kwargs["end_time"] == int(end.timestamp())
    mock_import.assert_awaited()
