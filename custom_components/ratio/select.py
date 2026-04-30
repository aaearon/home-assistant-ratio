"""Select platform for Ratio EV Charging.

Skeletons only — full implementation requires live payload to confirm
allowed-value sets and the exact mutator surface on RatioClient.
"""
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
    """Select for the charging mode (e.g. NORMAL / SOLAR / SCHEDULED).

    # TODO: populate options from UserSettings.charging_mode.allowed_values
    # once user_settings() is fetched at setup. Skeleton only — current_option
    # / async_select_option will be wired when client.set_user_settings()
    # lands.
    """

    _attr_translation_key = "charge_mode"
    _attr_name = "Charge mode"
    _attr_options: list[str] = []

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "charge_mode")

    @property
    def current_option(self) -> str | None:
        return None  # TODO: derive from cached UserSettings.

    async def async_select_option(self, option: str) -> None:
        # TODO: call client.set_user_settings once the API is implemented.
        _LOGGER.warning(
            "set_user_settings not yet implemented; ignoring charge_mode=%s", option
        )


class RatioActiveVehicleSelect(_RatioSelectBase):
    """Select for the active vehicle.

    # TODO: load vehicles via client.vehicles() at setup; map between
    # vehicle_id and vehicle_name for display.
    """

    _attr_translation_key = "active_vehicle"
    _attr_name = "Active vehicle"
    _attr_options: list[str] = []

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "active_vehicle")

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None or ov.charge_session_status is None:
            return None
        return ov.charge_session_status.vehicle_id

    async def async_select_option(self, option: str) -> None:
        # TODO: route through client.start_charge(vehicle_id=...) /
        # set_active_vehicle once the API is implemented.
        _LOGGER.warning(
            "active_vehicle setter not yet implemented; ignoring vehicle=%s", option
        )
