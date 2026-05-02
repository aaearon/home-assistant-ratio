"""Tests for Ratio service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from aioratio.models import Vehicle
from aioratio.models.history import Session, SessionHistoryPage, TimeData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import (
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

    # Only active_entry is in LOADED state
    active_entry._async_set_state(hass, ConfigEntryState.LOADED, None)

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
    coordinator = entry.runtime_data.coordinator
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
    history = entry.runtime_data.history_coordinator

    begin_dt = datetime(2023, 11, 1, tzinfo=UTC)
    end_dt = datetime(2023, 12, 1, tzinfo=UTC)

    mock_import_window = AsyncMock(return_value={"SN-IMP": 2})
    with patch.object(history, "async_import_window", new=mock_import_window):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_SESSION_HISTORY,
            {"begin_time": begin_dt.isoformat(), "end_time": end_dt.isoformat()},
            blocking=True,
            return_response=True,
        )

    assert response == {"imported": {"SN-IMP": 2}}
    mock_import_window.assert_awaited_once()
    call_kwargs = mock_import_window.await_args.kwargs
    assert int(call_kwargs["begin_time"].timestamp()) == int(begin_dt.timestamp())
    assert int(call_kwargs["end_time"].timestamp()) == int(end_dt.timestamp())


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
    history = entry.runtime_data.history_coordinator
    coordinator = entry.runtime_data.coordinator

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

    from custom_components.ratio.coordinator import RatioData

    coordinator.async_set_updated_data(
        RatioData(
            chargers={serial: ChargerOverview.from_dict({"serialNumber": serial})}
        )
    )

    async def _fake(hass, ser, sessions, starting_total):
        return float(starting_total) + sum(s.total_charging_energy for s in sessions)

    begin = datetime(2023, 11, 1, tzinfo=UTC)
    end = datetime(2023, 12, 1, tzinfo=UTC)

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


@pytest.mark.asyncio
async def test_start_charge_calls_client(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.start_charge should call client.start_charge for the device."""
    entry = setup_integration
    client = mock_ratio_client.return_value

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN-START")},
    )

    await hass.services.async_call(
        DOMAIN,
        "start_charge",
        {"device_id": device.id},
        blocking=True,
    )

    client.start_charge.assert_awaited_once()
    args = client.start_charge.await_args.args
    assert args[0] == "SN-START"


@pytest.mark.asyncio
async def test_start_charge_with_vehicle_id(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.start_charge should pass vehicle_id when provided."""
    entry = setup_integration
    client = mock_ratio_client.return_value

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN-VEH")},
    )

    await hass.services.async_call(
        DOMAIN,
        "start_charge",
        {"device_id": device.id, "vehicle_id": "v1"},
        blocking=True,
    )

    client.start_charge.assert_awaited_once()
    _, kwargs = client.start_charge.await_args
    assert kwargs.get("vehicle_id") == "v1"


@pytest.mark.asyncio
async def test_start_charge_uses_preferred_vehicle(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.start_charge should use preferred_vehicle when no explicit vehicle_id."""
    entry = setup_integration
    client = mock_ratio_client.return_value
    coordinator = entry.runtime_data.coordinator
    coordinator.preferred_vehicle["SN-PREF"] = "pref-v1"

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN-PREF")},
    )

    await hass.services.async_call(
        DOMAIN,
        "start_charge",
        {"device_id": device.id},
        blocking=True,
    )

    client.start_charge.assert_awaited_once()
    _, kwargs = client.start_charge.await_args
    assert kwargs.get("vehicle_id") == "pref-v1"


@pytest.mark.asyncio
async def test_stop_charge_calls_client(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """ratio.stop_charge should call client.stop_charge for the device."""
    entry = setup_integration
    client = mock_ratio_client.return_value

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN-STOP")},
    )

    await hass.services.async_call(
        DOMAIN,
        "stop_charge",
        {"device_id": device.id},
        blocking=True,
    )

    client.stop_charge.assert_awaited_once()
    args = client.stop_charge.await_args.args
    assert args[0] == "SN-STOP"


@pytest.mark.asyncio
async def test_resolve_serials_unknown_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """_resolve_serials should raise for an unknown device_id."""
    call = MagicMock()
    call.data = {"device_id": "nonexistent-device-id"}

    with pytest.raises(ServiceValidationError):
        _resolve_serials(hass, call)


@pytest.mark.asyncio
async def test_resolve_serials_non_ratio_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """_resolve_serials should raise for a device without ratio identifiers."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("other_integration", "SERIAL")},
    )

    call = MagicMock()
    call.data = {"device_id": device.id}

    with pytest.raises(ServiceValidationError):
        _resolve_serials(hass, call)


@pytest.mark.asyncio
async def test_add_vehicle_rate_limit_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """add_vehicle should raise HomeAssistantError on rate limit."""
    from aioratio.exceptions import RatioRateLimitError

    client = mock_ratio_client.return_value
    client.add_vehicle = AsyncMock(side_effect=RatioRateLimitError("too fast"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_VEHICLE,
            {"vehicle_name": "Test"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_add_vehicle_connection_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """add_vehicle should raise HomeAssistantError on connection error."""
    from aioratio.exceptions import RatioConnectionError

    client = mock_ratio_client.return_value
    client.add_vehicle = AsyncMock(side_effect=RatioConnectionError("timeout"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_VEHICLE,
            {"vehicle_name": "Test"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_add_vehicle_api_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """add_vehicle should raise HomeAssistantError on API error."""
    from aioratio.exceptions import RatioApiError

    client = mock_ratio_client.return_value
    client.add_vehicle = AsyncMock(side_effect=RatioApiError("server error"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_VEHICLE,
            {"vehicle_name": "Test"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_remove_vehicle_rate_limit_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """remove_vehicle should raise HomeAssistantError on rate limit."""
    from aioratio.exceptions import RatioRateLimitError

    client = mock_ratio_client.return_value
    client.remove_vehicle = AsyncMock(side_effect=RatioRateLimitError("too fast"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REMOVE_VEHICLE,
            {"vehicle_id": "v1"},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_remove_vehicle_connection_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """remove_vehicle should raise HomeAssistantError on connection error."""
    from aioratio.exceptions import RatioConnectionError

    client = mock_ratio_client.return_value
    client.remove_vehicle = AsyncMock(side_effect=RatioConnectionError("timeout"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REMOVE_VEHICLE,
            {"vehicle_id": "v1"},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_remove_vehicle_api_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """remove_vehicle should raise HomeAssistantError on API error."""
    from aioratio.exceptions import RatioApiError

    client = mock_ratio_client.return_value
    client.remove_vehicle = AsyncMock(side_effect=RatioApiError("server error"))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REMOVE_VEHICLE,
            {"vehicle_id": "v1"},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_import_session_history_rate_limit_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """import_session_history should raise HomeAssistantError on rate limit."""
    from aioratio.exceptions import RatioRateLimitError

    entry = setup_integration
    history = entry.runtime_data.history_coordinator

    with (
        patch.object(
            history,
            "async_import_window",
            new=AsyncMock(side_effect=RatioRateLimitError("too fast")),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_SESSION_HISTORY,
            {"begin_time": "2023-11-01T00:00:00+00:00"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_import_session_history_connection_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_ratio_client: MagicMock,
) -> None:
    """import_session_history should raise HomeAssistantError on connection error."""
    from aioratio.exceptions import RatioConnectionError

    entry = setup_integration
    history = entry.runtime_data.history_coordinator

    with (
        patch.object(
            history,
            "async_import_window",
            new=AsyncMock(side_effect=RatioConnectionError("timeout")),
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_SESSION_HISTORY,
            {"begin_time": "2023-11-01T00:00:00+00:00"},
            blocking=True,
            return_response=True,
        )
