"""Service handlers for the Ratio integration."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aioratio import BleClient, RatioClient
from aioratio.exceptions import (
    RatioApiError,
    RatioBleConnectionError,
    RatioBleError,
    RatioConnectionError,
    RatioRateLimitError,
)
from aioratio.models import ChargeSchedule, ScheduleSlot, Vehicle
from bleak.exc import BleakError
from homeassistant.components.bluetooth import async_discovered_service_info
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util.json import JsonValueType

from .const import (
    ATTR_BEGIN_TIME,
    ATTR_END_TIME,
    ATTR_LICENSE_PLATE,
    ATTR_SLOTS,
    ATTR_VEHICLE_ID,
    ATTR_VEHICLE_NAME,
    DOMAIN,
    SERVICE_ADD_VEHICLE,
    SERVICE_BLE_PROBE,
    SERVICE_IMPORT_SESSION_HISTORY,
    SERVICE_RECONFIGURE_WIFI,
    SERVICE_REMOVE_VEHICLE,
    SERVICE_SET_SCHEDULE,
    SERVICE_START_CHARGE,
    SERVICE_STOP_CHARGE,
)
from .coordinator import RatioCoordinator

if TYPE_CHECKING:
    from . import RatioRuntimeData

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

# Accept both zero-padded ("07:00") and single-digit-hour ("7:00") forms;
# the previous schema accepted any string and aioratio happily parsed both,
# so refusing "7:00" outright would break existing automations.
_HHMM_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")


def _normalize_hhmm(value: Any) -> str:
    """Validate ``H:MM``/``HH:MM`` and return the zero-padded ``HH:MM`` form."""
    s = cv.string(value)
    if not _HHMM_RE.match(s):
        raise vol.Invalid(f"time {value!r} must be HH:MM (00:00-23:59)")
    h, m = s.split(":")
    return f"{int(h):02d}:{m}"


# Accept both full English day names and 3-letter abbreviations; ``aioratio``
# normalises them to lower-case full names internally.
_VALID_DAYS = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
    }
)


def _validated_day(value: Any) -> str:
    s = cv.string(value).strip().lower()
    if s not in _VALID_DAYS:
        raise vol.Invalid(
            f"unknown day {value!r}; expected one of "
            "monday/tuesday/.../sunday or mon/tue/.../sun"
        )
    return s


def _slot_has_start_and_end(slot: dict[str, Any]) -> dict[str, Any]:
    """Reject slots that omit both ``start``/``startTime`` or both ``end``/``endTime``.

    The cloud DTO requires both ends. Without this guard the failure surfaces
    only later, inside ``ScheduleSlot.to_dict()``, with a less clear message.
    """
    if "start" not in slot and "startTime" not in slot:
        raise vol.Invalid("slot must have a start time (start or startTime)")
    if "end" not in slot and "endTime" not in slot:
        raise vol.Invalid("slot must have an end time (end or endTime)")
    return slot


SLOT_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("start"): _normalize_hhmm,
            vol.Optional("startTime"): _normalize_hhmm,
            vol.Optional("end"): _normalize_hhmm,
            vol.Optional("endTime"): _normalize_hhmm,
            vol.Optional("days"): vol.All(cv.ensure_list, [_validated_day]),
        }
    ),
    _slot_has_start_and_end,
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

RECONFIGURE_WIFI_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("ssid"): cv.string,
        vol.Optional("password"): cv.string,
    }
)

BLE_PROBE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})


def _resolve_serials(hass: HomeAssistant, call: ServiceCall) -> list[tuple[str, str]]:
    """Resolve target devices to (entry_id, serial) pairs."""
    device_ids = call.data.get("device_id") or []
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    loaded_ids = {e.entry_id for e in hass.config_entries.async_loaded_entries(DOMAIN)}

    device_reg = dr.async_get(hass)
    pairs: list[tuple[str, str]] = []
    for dev_id in device_ids:
        device = device_reg.async_get(dev_id)
        if device is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
                translation_placeholders={"device_id": dev_id},
            )
        serial: str | None = None
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                serial = ident[1]
                break
        if serial is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_ratio_charger",
                translation_placeholders={"device_id": dev_id},
            )

        entry_id = next(
            (eid for eid in device.config_entries if eid in loaded_ids),
            None,
        )
        if entry_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_active_config_entry",
                translation_placeholders={"device_id": dev_id},
            )
        pairs.append((entry_id, serial))
    return pairs


def _client_and_coordinator(
    hass: HomeAssistant, entry_id: str
) -> tuple[RatioClient, RatioCoordinator]:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="config_entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    return entry.runtime_data.client, entry.runtime_data.coordinator


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


def _all_entries(hass: HomeAssistant) -> list[RatioRuntimeData]:
    """Return all runtime data for loaded integration entries."""
    return [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]


def _single_entry(hass: HomeAssistant) -> RatioRuntimeData:
    """Return the only entry's runtime data, or raise if 0 or >1 are loaded."""
    entries = _all_entries(hass)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_config_entry_loaded",
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="multiple_accounts",
        )
    return entries[0]


async def _handle_add_vehicle(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    runtime = _single_entry(hass)
    client = runtime.client
    coordinator = runtime.coordinator
    vehicle = Vehicle(
        vehicle_name=call.data[ATTR_VEHICLE_NAME],
        license_plate=call.data.get(ATTR_LICENSE_PLATE),
    )
    try:
        created = await client.add_vehicle(vehicle)
    except RatioRateLimitError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"command": "add_vehicle", "error": str(err)},
        ) from err
    except RatioConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="connection_error",
            translation_placeholders={"command": "add_vehicle", "error": str(err)},
        ) from err
    except RatioApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"command": "add_vehicle", "error": str(err)},
        ) from err
    await coordinator.async_request_refresh()
    return {"vehicle_id": created.vehicle_id}


async def _handle_remove_vehicle(hass: HomeAssistant, call: ServiceCall) -> None:
    runtime = _single_entry(hass)
    client = runtime.client
    coordinator = runtime.coordinator
    vehicle_id: str = call.data[ATTR_VEHICLE_ID]
    try:
        await client.remove_vehicle(vehicle_id)
    except RatioRateLimitError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"command": "remove_vehicle", "error": str(err)},
        ) from err
    except RatioConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="connection_error",
            translation_placeholders={"command": "remove_vehicle", "error": str(err)},
        ) from err
    except RatioApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"command": "remove_vehicle", "error": str(err)},
        ) from err
    await coordinator.async_request_refresh()

    # Drop any preferred_vehicle entries that pointed at the removed vehicle,
    # holding the coordinator's prefs lock for the entire mutate+save so the
    # change can't interleave with a concurrent select-option update.
    await coordinator.async_remove_preferred_vehicle_id(vehicle_id)


async def _handle_import_session_history(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    runtime = _single_entry(hass)
    history = runtime.history_coordinator
    begin_time = call.data[ATTR_BEGIN_TIME]
    end_time = call.data.get(ATTR_END_TIME)
    try:
        imported = await history.async_import_window(
            begin_time=begin_time, end_time=end_time
        )
    except RatioRateLimitError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={
                "command": "import_session_history",
                "error": str(err),
            },
        ) from err
    except RatioConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="connection_error",
            translation_placeholders={
                "command": "import_session_history",
                "error": str(err),
            },
        ) from err
    except RatioApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={
                "command": "import_session_history",
                "error": str(err),
            },
        ) from err
    # Widen the inner value to JsonValueType so the outer dict matches
    # the invariant ServiceResponse (dict[str, JsonValueType] | None).
    inner: JsonValueType = dict(imported)
    return {"imported": inner}


async def _handle_set_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    raw_slots = call.data[ATTR_SLOTS] or []
    slots = [ScheduleSlot.from_dict(s) for s in raw_slots]
    schedule = ChargeSchedule(enabled=True, slots=slots)
    for entry_id, serial in _resolve_serials(hass, call):
        client, coordinator = _client_and_coordinator(hass, entry_id)
        await coordinator.request_command(client.set_charge_schedule, serial, schedule)


async def _handle_reconfigure_wifi(hass: HomeAssistant, call: ServiceCall) -> None:
    """Reconnect a Ratio charger to a Wi-Fi network via BLE."""
    pairs = _resolve_serials(hass, call)
    entry_id, serial = pairs[0]

    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="config_entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )

    ble_coordinators: dict[str, Any] = getattr(
        entry.runtime_data, "ble_coordinators", {}
    )
    coordinator = ble_coordinators.get(serial)
    if coordinator is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ble_not_enabled",
            translation_placeholders={"serial": serial},
        )

    ssid: str = call.data["ssid"]
    # aioratio.wifi_connect emits a RuntimeWarning whenever password is not
    # None (including ""), so pass None for open networks rather than the
    # empty string the service schema would otherwise yield.
    password: str | None = call.data.get("password") or None

    ble_device = coordinator._pick_best_device()
    if ble_device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ble_connect_failed",
            translation_placeholders={"serial": serial, "error": "device not found"},
        )

    try:
        async with coordinator._wifi_lock:
            client = BleClient(ble_device)
            try:
                async with client:
                    scan_results = await client.wifi_scan()
                    found_ssids = {ap.ssid for ap in scan_results}
                    if ssid not in found_ssids:
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="ssid_not_found",
                            translation_placeholders={"ssid": ssid},
                        )
                    await client.wifi_connect(ssid, password)
            except ServiceValidationError:
                raise
    except (RatioBleConnectionError, RatioBleError, BleakError) as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ble_connect_failed",
            translation_placeholders={"serial": serial, "error": str(err)},
        ) from err


async def _handle_ble_probe(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Diagnostic: connect to the charger via the strongest current advert.

    Bypasses ``RatioBleCoordinator.address`` to test whether a BT proxy (which
    only sees rotating RPAs) can complete a GATT handshake. Returns a
    structured response listing every advert seen for ``RATIO_<serial>``,
    which scanner is serving it, and the outcome of a single connect+read.
    """
    pairs = _resolve_serials(hass, call)
    entry_id, serial = pairs[0]
    local_name = f"RATIO_{serial}"

    candidates = []
    for info in async_discovered_service_info(hass, connectable=True):
        if info.name != local_name:
            continue
        scanner = getattr(info.device, "scanner", None)
        scanner_class = type(scanner).__name__ if scanner is not None else None
        candidates.append(
            {
                "address": info.address,
                "rssi": info.rssi,
                "scanner_source": info.source,
                "scanner_class": scanner_class,
                "_info": info,
            }
        )
    candidates.sort(key=lambda c: c["rssi"] or -127, reverse=True)

    response: dict[str, JsonValueType] = {
        "serial": serial,
        "entry_id": entry_id,
        "local_name": local_name,
        "candidates": [{k: v for k, v in c.items() if k != "_info"} for c in candidates],
    }

    if not candidates:
        response["status"] = "no_advert"
        return response

    best = candidates[0]
    response["chosen"] = {k: v for k, v in best.items() if k != "_info"}

    client = BleClient(best["_info"].device)
    try:
        async with client:
            await client.get_charger_sensor_values()
    except Exception as err:  # noqa: BLE001 — diagnostic surfaces every failure mode
        response["status"] = "error"
        response["error_type"] = type(err).__name__
        response["error"] = str(err)
        return response

    response["status"] = "ok"
    return response


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

    async def reconfigure_wifi(call: ServiceCall) -> None:
        await _handle_reconfigure_wifi(hass, call)

    async def ble_probe(call: ServiceCall) -> ServiceResponse:
        return await _handle_ble_probe(hass, call)

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECONFIGURE_WIFI,
        reconfigure_wifi,
        schema=RECONFIGURE_WIFI_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BLE_PROBE,
        ble_probe,
        schema=BLE_PROBE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
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
        SERVICE_RECONFIGURE_WIFI,
        SERVICE_BLE_PROBE,
    ):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
