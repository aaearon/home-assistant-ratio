"""Binary sensor platform for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aioratio.models import ChargerOverview

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator


@dataclass(frozen=True, kw_only=True)
class RatioBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Ratio binary sensor."""

    value_fn: Callable[[ChargerOverview], bool | None]


def _ind(ov: ChargerOverview):
    return (
        ov.charger_status.indicators
        if ov.charger_status is not None and ov.charger_status.indicators is not None
        else None
    )


BINARY_SENSOR_DESCRIPTIONS: tuple[RatioBinarySensorEntityDescription, ...] = (
    RatioBinarySensorEntityDescription(
        key="vehicle_connected",
        translation_key="vehicle_connected",
        name="Vehicle connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda ov: (_ind(ov).is_vehicle_connected if _ind(ov) else None),
    ),
    RatioBinarySensorEntityDescription(
        key="charge_session_active",
        translation_key="charge_session_active",
        name="Charging",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda ov: (_ind(ov).is_charge_session_active if _ind(ov) else None),
    ),
    RatioBinarySensorEntityDescription(
        key="charging_paused",
        translation_key="charging_paused",
        name="Charging paused",
        value_fn=lambda ov: (_ind(ov).is_charging_paused if _ind(ov) else None),
    ),
    RatioBinarySensorEntityDescription(
        key="error",
        translation_key="error",
        name="Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda ov: (
            bool(_ind(ov).errors) if _ind(ov) is not None else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio binary sensors from a config entry."""
    coordinator: RatioCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[RatioBinarySensor] = []
    serials = coordinator.data.chargers if coordinator.data else {}
    for serial in serials:
        for desc in BINARY_SENSOR_DESCRIPTIONS:
            entities.append(RatioBinarySensor(coordinator, serial, desc))
    async_add_entities(entities)


class RatioBinarySensor(CoordinatorEntity[RatioCoordinator], BinarySensorEntity):
    """A single binary sensor backed by the Ratio coordinator."""

    entity_description: RatioBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RatioCoordinator,
        serial: str,
        description: RatioBinarySensorEntityDescription,
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
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None:
            return None
        try:
            return self.entity_description.value_fn(ov)
        except AttributeError:
            return None
