"""Service handlers for the Ratio integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from aioratio import RatioClient
from aioratio.models import ChargeSchedule, ScheduleSlot

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    ATTR_SLOTS,
    ATTR_VEHICLE_ID,
    DOMAIN,
    SERVICE_SET_SCHEDULE,
    SERVICE_START_CHARGE,
    SERVICE_STOP_CHARGE,
)
from .coordinator import RatioCoordinator

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

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Required(ATTR_SLOTS): list,
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

        entry_id = next(iter(device.config_entries), None)
        if entry_id is None or entry_id not in hass.data.get(DOMAIN, {}):
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
    vehicle_id = call.data.get(ATTR_VEHICLE_ID)
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        kwargs: dict[str, Any] = {}
        if vehicle_id is not None:
            kwargs["vehicle_id"] = vehicle_id
        await coordinator.request_command(client.start_charge, serial, **kwargs)


async def _handle_stop_charge(hass: HomeAssistant, call: ServiceCall) -> None:
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        await coordinator.request_command(client.stop_charge, serial)


async def _handle_set_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    raw_slots = call.data[ATTR_SLOTS] or []
    slots = [ScheduleSlot.from_dict(s) for s in raw_slots if isinstance(s, dict)]
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


async def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Ratio services."""
    for svc in (SERVICE_START_CHARGE, SERVICE_STOP_CHARGE, SERVICE_SET_SCHEDULE):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
