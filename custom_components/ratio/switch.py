"""Switch platform for Ratio EV Charging."""
from __future__ import annotations

import logging
from typing import Any

from aioratio import RatioClient

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio switches from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RatioCoordinator = data["coordinator"]
    client: RatioClient = data["client"]
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities = [RatioChargingSwitch(coordinator, client, serial) for serial in new]
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class RatioChargingSwitch(CoordinatorEntity[RatioCoordinator], SwitchEntity):
    """Switch that starts / stops the active charge session."""

    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    _attr_name = "Charging"

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{serial}_charging_switch"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None or ov.charger_status is None:
            return None
        ind = ov.charger_status.indicators
        if ind is None:
            return None
        return bool(ind.is_charge_session_active)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a charge session."""
        if self.is_on is True:
            return
        status = self._charger_status()
        if status is not None and not status.is_charge_start_allowed:
            ind = status.indicators
            state = ind.charging_state if ind is not None else "unknown"
            raise HomeAssistantError(
                f"charger reports start not allowed (state={state})"
            )
        call_kwargs: dict[str, Any] = {}
        preferred = self.coordinator.preferred_vehicle.get(self._serial)
        if preferred is not None:
            call_kwargs["vehicle_id"] = preferred
        await self.coordinator.request_command(
            self._client.start_charge, self._serial, **call_kwargs
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the active charge session."""
        if self.is_on is False:
            return
        status = self._charger_status()
        if status is not None and not status.is_charge_stop_allowed:
            ind = status.indicators
            state = ind.charging_state if ind is not None else "unknown"
            raise HomeAssistantError(
                f"charger reports stop not allowed (state={state})"
            )
        await self.coordinator.request_command(
            self._client.stop_charge, self._serial
        )

    def _charger_status(self):
        if self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        return ov.charger_status if ov is not None else None
