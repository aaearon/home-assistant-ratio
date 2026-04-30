"""Sensor platform for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aioratio.models import ChargerOverview

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator


@dataclass(frozen=True, kw_only=True)
class RatioSensorEntityDescription(SensorEntityDescription):
    """Describes a Ratio sensor."""

    value_fn: Callable[[ChargerOverview], Any]


SENSOR_DESCRIPTIONS: tuple[RatioSensorEntityDescription, ...] = (
    RatioSensorEntityDescription(
        key="actual_charging_power",
        translation_key="actual_charging_power",
        name="Charging power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ov: (
            ov.charge_session_status.actual_charging_power
            if ov.charge_session_status is not None
            else None
        ),
    ),
    RatioSensorEntityDescription(
        key="cloud_connection_state",
        translation_key="cloud_connection_state",
        name="Cloud connection state",
        value_fn=lambda ov: ov.cloud_connection_state,
    ),
    RatioSensorEntityDescription(
        key="charging_state",
        translation_key="charging_state",
        name="Charging state",
        value_fn=lambda ov: (
            ov.charger_status.indicators.charging_state
            if ov.charger_status is not None and ov.charger_status.indicators is not None
            else None
        ),
    ),
)


def _build_sensor_entities(
    coordinator: RatioCoordinator, serial: str
) -> list[RatioSensor]:
    return [RatioSensor(coordinator, serial, desc) for desc in SENSOR_DESCRIPTIONS]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio sensors from a config entry."""
    coordinator: RatioCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities: list[RatioSensor] = []
        for serial in new:
            entities.extend(_build_sensor_entities(coordinator, serial))
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class RatioSensor(CoordinatorEntity[RatioCoordinator], SensorEntity):
    """A single sensor backed by the Ratio coordinator."""

    entity_description: RatioSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RatioCoordinator,
        serial: str,
        description: RatioSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )

    @property
    def _overview(self) -> ChargerOverview | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.chargers.get(self._serial)

    @property
    def native_value(self) -> Any:
        ov = self._overview
        if ov is None:
            return None
        try:
            return self.entity_description.value_fn(ov)
        except AttributeError:
            return None

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
