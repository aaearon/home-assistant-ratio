"""Diagnostics support for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import RatioConfigEntry

TO_REDACT = {
    "email",
    "password",
    "access_token",
    "id_token",
    "refresh_token",
    "device_key",
    "device_password",
    "device_group_key",
    "serial_number",
    "serialNumber",
    "license_plate",
    "licensePlate",
}


def _to_jsonable(obj: Any) -> Any:
    """Convert dataclasses (and nested containers) to plain dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RatioConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    # async_redact_data redacts by field name, not by dict key. Coordinator
    # data is keyed by charger serial, so emit as a list to avoid leaking
    # serials as top-level keys; serial_number inside each entry is in
    # TO_REDACT and gets redacted normally.
    data = coordinator.data
    chargers = [_to_jsonable(ov) for ov in data.chargers.values()] if data else []
    user_settings = (
        [_to_jsonable(s) for s in data.user_settings.values()] if data else []
    )
    solar_settings = (
        [_to_jsonable(s) for s in data.solar_settings.values()] if data else []
    )
    vehicles = [_to_jsonable(v) for v in data.vehicles] if data else []
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_data": async_redact_data(
            {
                "chargers": chargers,
                "user_settings": user_settings,
                "solar_settings": solar_settings,
                "vehicles": vehicles,
            },
            TO_REDACT,
        ),
    }
