"""Diagnostics support for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import RatioCoordinator

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
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: RatioCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    raw_data = {
        serial: _to_jsonable(ov) for serial, ov in (coordinator.data or {}).items()
    }
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_data": async_redact_data(raw_data, TO_REDACT),
    }
