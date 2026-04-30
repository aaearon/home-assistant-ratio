"""Select platform for Ratio EV Charging."""
from __future__ import annotations

import logging

from aioratio import RatioClient

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator

_LOGGER = logging.getLogger(__name__)

# The cloud may omit allowedValues for chargingMode; this fallback mirrors
# the modes seen in the APK. Update if Ratio adds modes.
_CHARGE_MODE_FALLBACK = ["FAST", "SOLAR", "SMART_SOLAR"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio selects from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RatioCoordinator = data["coordinator"]
    client: RatioClient = data["client"]

    entities: list[CoordinatorEntity] = []
    serials = coordinator.data.chargers if coordinator.data else {}
    for serial in serials:
        entities.append(RatioChargeModeSelect(coordinator, client, serial))
        entities.append(RatioActiveVehicleSelect(coordinator, client, serial))
    async_add_entities(entities)


class _RatioSelectBase(CoordinatorEntity[RatioCoordinator], SelectEntity):
    """Common boilerplate for Ratio select entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )


class RatioChargeModeSelect(_RatioSelectBase):
    """Select for the charging mode."""

    _attr_translation_key = "charge_mode"
    _attr_name = "Charge mode"

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "charge_mode")

    @property
    def options(self) -> list[str]:
        if self.coordinator.data is None:
            return list(_CHARGE_MODE_FALLBACK)
        settings = self.coordinator.data.user_settings.get(self._serial)
        if settings is None or settings.charging_mode is None:
            return list(_CHARGE_MODE_FALLBACK)
        return settings.charging_mode.allowed_values or list(_CHARGE_MODE_FALLBACK)

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        settings = self.coordinator.data.user_settings.get(self._serial)
        if settings is None or settings.charging_mode is None:
            return None
        return settings.charging_mode.value

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.request_command(
            self._client.set_user_settings,
            self._serial,
            {"chargingMode": {"value": option}},
        )


class RatioActiveVehicleSelect(_RatioSelectBase):
    """Select for the active vehicle.

    The cloud has no per-charger "default vehicle" setting; this is a
    HA-side preference that is passed to the next start_charge call.
    Held in memory on the coordinator (lost on HA restart).
    """

    _attr_translation_key = "active_vehicle"
    _attr_name = "Active vehicle"

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "active_vehicle")

    def _name_for(self, vehicle_id: str | None) -> str | None:
        if vehicle_id is None or self.coordinator.data is None:
            return None
        for v in self.coordinator.data.vehicles:
            if v.vehicle_id == vehicle_id:
                return v.vehicle_name or v.vehicle_id
        return vehicle_id

    @property
    def options(self) -> list[str]:
        if self.coordinator.data is None:
            return []
        return [
            (v.vehicle_name or v.vehicle_id)
            for v in self.coordinator.data.vehicles
            if v.vehicle_id is not None
        ]

    @property
    def current_option(self) -> str | None:
        preferred = self.coordinator.preferred_vehicle.get(self._serial)
        if preferred is not None:
            return self._name_for(preferred)
        if self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None or ov.charge_session_status is None:
            return None
        return self._name_for(ov.charge_session_status.vehicle_id)

    async def async_select_option(self, option: str) -> None:
        if self.coordinator.data is None:
            return
        for v in self.coordinator.data.vehicles:
            display = v.vehicle_name or v.vehicle_id
            if display == option and v.vehicle_id is not None:
                self.coordinator.preferred_vehicle[self._serial] = v.vehicle_id
                self.async_write_ha_state()
                return
        _LOGGER.warning("active_vehicle option %s did not match any known vehicle", option)
