"""Service handlers for the Ratio integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aioratio import RatioClient
from aioratio.exceptions import (
    RatioApiError,
    RatioConnectionError,
    RatioRateLimitError,
)
from aioratio.models import ChargeSchedule, ScheduleSlot, Vehicle

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    ATTR_BEGIN_TIME,
    ATTR_END_TIME,
    ATTR_LICENSE_PLATE,
    ATTR_SLOTS,
    ATTR_VEHICLE_ID,
    ATTR_VEHICLE_NAME,
    DOMAIN,
    SERVICE_ADD_VEHICLE,
    SERVICE_IMPORT_SESSION_HISTORY,
    SERVICE_REMOVE_VEHICLE,
    SERVICE_SET_SCHEDULE,
    SERVICE_START_CHARGE,
    SERVICE_STOP_CHARGE,
)
from .coordinator import RatioCoordinator, RatioHistoryCoordinator

_LOGGER = logging.getLogger(__name__)

START_CHARGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_VEHICLE_ID): cv.string,
    }
)

STOP_CHARGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Any(cv.string, [cv.string]),
    }
)

SLOT_SCHEMA = vol.Schema(
    {
        vol.Optional("start"): cv.string,
        vol.Optional("startTime"): cv.string,
        vol.Optional("end"): cv.string,
        vol.Optional("endTime"): cv.string,
        vol.Optional("days"): vol.All(cv.ensure_list, [cv.string]),
    }
)

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Required(ATTR_SLOTS): vol.All(cv.ensure_list, [SLOT_SCHEMA]),
    }
)

ADD_VEHICLE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VEHICLE_NAME): cv.string,
        vol.Optional(ATTR_LICENSE_PLATE): cv.string,
    }
)

REMOVE_VEHICLE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VEHICLE_ID): cv.string,
    }
)

IMPORT_SESSION_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BEGIN_TIME): cv.datetime,
        vol.Optional(ATTR_END_TIME): cv.datetime,
    }
)


def _resolve_serials(hass: HomeAssistant, call: ServiceCall) -> list[tuple[str, str]]:
    """Resolve target devices to (entry_id, serial) pairs."""
    device_ids = call.data.get("device_id") or []
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    device_reg = dr.async_get(hass)
    pairs: list[tuple[str, str]] = []
    for dev_id in device_ids:
        device = device_reg.async_get(dev_id)
        if device is None:
            raise HomeAssistantError(f"Unknown device {dev_id}")
        serial: str | None = None
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                serial = ident[1]
                break
        if serial is None:
            raise HomeAssistantError(f"Device {dev_id} is not a Ratio charger")

        entry_id = next(
            (eid for eid in device.config_entries if eid in hass.data.get(DOMAIN, {})),
            None,
        )
        if entry_id is None:
            raise HomeAssistantError(
                f"No active Ratio config entry for device {dev_id}"
            )
        pairs.append((entry_id, serial))
    return pairs


def _client_and_coordinator(
    hass: HomeAssistant, entry_id: str
) -> tuple[RatioClient, RatioCoordinator]:
    data = hass.data[DOMAIN][entry_id]
    return data["client"], data["coordinator"]


async def _handle_start_charge(hass: HomeAssistant, call: ServiceCall) -> None:
    explicit_vehicle = call.data.get(ATTR_VEHICLE_ID)
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        kwargs: dict[str, Any] = {}
        vehicle_id = explicit_vehicle or coordinator.preferred_vehicle.get(serial)
        if vehicle_id is not None:
            kwargs["vehicle_id"] = vehicle_id
        await coordinator.request_command(client.start_charge, serial, **kwargs)


async def _handle_stop_charge(hass: HomeAssistant, call: ServiceCall) -> None:
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        await coordinator.request_command(client.stop_charge, serial)


def _all_entries(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return all per-entry data dicts for the integration."""
    return list(hass.data.get(DOMAIN, {}).values())


def _single_entry(hass: HomeAssistant) -> dict[str, Any]:
    """Return the only entry's data dict, or raise if 0 or >1 are loaded."""
    entries = _all_entries(hass)
    if not entries:
        raise HomeAssistantError("No Ratio config entry is loaded")
    if len(entries) > 1:
        raise HomeAssistantError(
            "Multiple Ratio accounts are configured; this service does not "
            "yet support targeting a specific account"
        )
    return entries[0]


async def _handle_add_vehicle(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    data = _single_entry(hass)
    client: RatioClient = data["client"]
    coordinator: RatioCoordinator = data["coordinator"]
    vehicle = Vehicle(
        vehicle_name=call.data[ATTR_VEHICLE_NAME],
        license_plate=call.data.get(ATTR_LICENSE_PLATE),
    )
    try:
        created = await client.add_vehicle(vehicle)
    except RatioRateLimitError as err:
        raise HomeAssistantError(f"add_vehicle: rate limited: {err}") from err
    except RatioConnectionError as err:
        raise HomeAssistantError(f"add_vehicle: connection error: {err}") from err
    except RatioApiError as err:
        raise HomeAssistantError(f"add_vehicle failed: {err}") from err
    await coordinator.async_request_refresh()
    return {"vehicle_id": created.vehicle_id}


async def _handle_remove_vehicle(hass: HomeAssistant, call: ServiceCall) -> None:
    data = _single_entry(hass)
    client: RatioClient = data["client"]
    coordinator: RatioCoordinator = data["coordinator"]
    vehicle_id: str = call.data[ATTR_VEHICLE_ID]
    try:
        await client.remove_vehicle(vehicle_id)
    except RatioRateLimitError as err:
        raise HomeAssistantError(f"remove_vehicle: rate limited: {err}") from err
    except RatioConnectionError as err:
        raise HomeAssistantError(
            f"remove_vehicle: connection error: {err}"
        ) from err
    except RatioApiError as err:
        raise HomeAssistantError(f"remove_vehicle failed: {err}") from err
    await coordinator.async_request_refresh()

    # Drop any preferred_vehicle entries that pointed at the removed vehicle.
    stale = [s for s, vid in coordinator.preferred_vehicle.items() if vid == vehicle_id]
    if stale:
        for s in stale:
            coordinator.preferred_vehicle.pop(s, None)
        await coordinator.async_save_preferences()


async def _handle_import_session_history(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    data = _single_entry(hass)
    history: RatioHistoryCoordinator = data["history_coordinator"]
    begin_time = call.data[ATTR_BEGIN_TIME]
    end_time = call.data.get(ATTR_END_TIME)
    try:
        imported = await history.async_import_window(
            begin_time=begin_time, end_time=end_time
        )
    except RatioRateLimitError as err:
        raise HomeAssistantError(
            f"import_session_history: rate limited: {err}"
        ) from err
    except RatioConnectionError as err:
        raise HomeAssistantError(
            f"import_session_history: connection error: {err}"
        ) from err
    except RatioApiError as err:
        raise HomeAssistantError(f"import_session_history failed: {err}") from err
    return {"imported": imported}


async def _handle_set_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    raw_slots = call.data[ATTR_SLOTS] or []
    slots = [ScheduleSlot.from_dict(s) for s in raw_slots]
    schedule = ChargeSchedule(enabled=True, slots=slots)
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        # TODO: confirm against client.set_charge_schedule signature once
        # implemented.
        setter = getattr(client, "set_charge_schedule", None)
        if setter is None:
            raise HomeAssistantError(
                "set_charge_schedule is not supported by the installed aioratio version"
            )
        await coordinator.request_command(setter, serial, schedule)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Ratio services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_START_CHARGE):
        return

    async def start(call: ServiceCall) -> None:
        await _handle_start_charge(hass, call)

    async def stop(call: ServiceCall) -> None:
        await _handle_stop_charge(hass, call)

    async def set_schedule(call: ServiceCall) -> None:
        await _handle_set_schedule(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_START_CHARGE, start, schema=START_CHARGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_CHARGE, stop, schema=STOP_CHARGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, set_schedule, schema=SET_SCHEDULE_SCHEMA
    )

    async def add_vehicle(call: ServiceCall) -> ServiceResponse:
        return await _handle_add_vehicle(hass, call)

    async def remove_vehicle(call: ServiceCall) -> None:
        await _handle_remove_vehicle(hass, call)

    async def import_session_history(call: ServiceCall) -> ServiceResponse:
        return await _handle_import_session_history(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_VEHICLE,
        add_vehicle,
        schema=ADD_VEHICLE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_VEHICLE,
        remove_vehicle,
        schema=REMOVE_VEHICLE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_SESSION_HISTORY,
        import_session_history,
        schema=IMPORT_SESSION_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Ratio services."""
    for svc in (
        SERVICE_START_CHARGE,
        SERVICE_STOP_CHARGE,
        SERVICE_SET_SCHEDULE,
        SERVICE_ADD_VEHICLE,
        SERVICE_REMOVE_VEHICLE,
        SERVICE_IMPORT_SESSION_HISTORY,
    ):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
