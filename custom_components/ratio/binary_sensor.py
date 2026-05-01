"""Binary sensor platform for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from aioratio.models import ChargerOverview

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RatioConfigEntry
from .const import DOMAIN
from .coordinator import RatioCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RatioBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Ratio binary sensor."""

    value_fn: Callable[[ChargerOverview], bool | None]
    attrs_fn: Callable[[ChargerOverview], dict[str, Any]] | None = None


def _ind(ov: ChargerOverview) -> Any:
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
    RatioBinarySensorEntityDescription(
        key="charging_disabled",
        translation_key="charging_disabled",
        name="Charging disabled",
        value_fn=lambda ov: (_ind(ov).is_charging_disabled if _ind(ov) else None),
        attrs_fn=lambda ov: (
            {"reason": _ind(ov).is_charging_disabled_reason}
            if _ind(ov) is not None and _ind(ov).is_charging_disabled_reason is not None
            else {}
        ),
    ),
    RatioBinarySensorEntityDescription(
        key="charging_authorized",
        translation_key="charging_authorized",
        name="Charging authorized",
        value_fn=lambda ov: (_ind(ov).is_charging_authorized if _ind(ov) else None),
    ),
    RatioBinarySensorEntityDescription(
        key="power_reduced_by_dso",
        translation_key="power_reduced_by_dso",
        name="Power reduced by DSO",
        value_fn=lambda ov: (_ind(ov).is_power_reduced_by_dso if _ind(ov) else None),
    ),
)


def _fw(ov: ChargerOverview) -> Any:
    return ov.charger_firmware_status


FIRMWARE_BINARY_SENSOR_DESCRIPTIONS: tuple[RatioBinarySensorEntityDescription, ...] = (
    RatioBinarySensorEntityDescription(
        key="firmware_update_available",
        translation_key="firmware_update_available",
        name="Firmware update available",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda ov: (
            _fw(ov).is_firmware_update_available if _fw(ov) is not None else None
        ),
    ),
    RatioBinarySensorEntityDescription(
        key="firmware_update_allowed",
        translation_key="firmware_update_allowed",
        name="Firmware update allowed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda ov: (
            _fw(ov).is_firmware_update_allowed if _fw(ov) is not None else None
        ),
    ),
)


def _build_binary_sensor_entities(
    coordinator: RatioCoordinator, serial: str
) -> list["RatioBinarySensor"]:
    return [
        RatioBinarySensor(coordinator, serial, desc)
        for desc in (*BINARY_SENSOR_DESCRIPTIONS, *FIRMWARE_BINARY_SENSOR_DESCRIPTIONS)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RatioConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio binary sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return  # type: ignore[unreachable]
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities: list[RatioBinarySensor] = []
        for serial in new:
            entities.extend(_build_binary_sensor_entities(coordinator, serial))
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


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
            return None  # type: ignore[unreachable]
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None:
            return None
        try:
            return self.entity_description.value_fn(ov)
        except AttributeError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or self.coordinator.data is None:
            return None
        ov = self.coordinator.data.chargers.get(self._serial)
        if ov is None:
            return None
        try:
            return attrs_fn(ov) or None
        except AttributeError:
            return None
