"""Sensor platform for Ratio EV Charging."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from aioratio.models import ChargerOverview
from aioratio.models.history import Session

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator, RatioHistoryCoordinator


@dataclass(kw_only=True)
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


def _fw(ov: ChargerOverview):
    return ov.charger_firmware_status


FIRMWARE_SENSOR_DESCRIPTIONS: tuple[RatioSensorEntityDescription, ...] = (
    RatioSensorEntityDescription(
        key="firmware_update_status",
        translation_key="firmware_update_status",
        name="Firmware update status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda ov: (
            _fw(ov).firmware_update_status if _fw(ov) is not None else None
        ),
    ),
)


@dataclass(kw_only=True)
class RatioLastSessionSensorDescription(SensorEntityDescription):
    """Describes a last-session sensor backed by the history coordinator."""

    value_fn: Callable[[Session], Any]


def _last_session(history: RatioHistoryCoordinator, serial: str) -> Session | None:
    data = getattr(history, "data", None)
    if not data:
        return None
    sessions = data.get(serial)
    if not sessions:
        return None
    # Sessions are stored sorted ascending by begin time.
    return sessions[-1]


def _ts(epoch: int | None) -> datetime | None:
    if epoch is None or epoch <= 0:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _session_duration(s: Session) -> int | None:
    if s.begin is None or s.end is None:
        return None
    if not s.begin.time or not s.end.time:
        return None
    return int(s.end.time) - int(s.begin.time)


LAST_SESSION_DESCRIPTIONS: tuple[RatioLastSessionSensorDescription, ...] = (
    RatioLastSessionSensorDescription(
        key="last_session_energy",
        translation_key="last_session_energy",
        name="Last session energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        # Intentionally NO state_class — value resets each session, not monotonic.
        value_fn=lambda s: s.total_charging_energy,
    ),
    RatioLastSessionSensorDescription(
        key="last_session_duration",
        translation_key="last_session_duration",
        name="Last session duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=_session_duration,
    ),
    RatioLastSessionSensorDescription(
        key="last_session_started_at",
        translation_key="last_session_started_at",
        name="Last session started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _ts(s.begin.time if s.begin else None),
    ),
    RatioLastSessionSensorDescription(
        key="last_session_ended_at",
        translation_key="last_session_ended_at",
        name="Last session ended",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _ts(s.end.time if s.end else None),
    ),
    RatioLastSessionSensorDescription(
        key="last_session_vehicle",
        translation_key="last_session_vehicle",
        name="Last session vehicle",
        value_fn=lambda s: (s.vehicle.vehicle_name if s.vehicle else None),
    ),
)


def _build_sensor_entities(
    coordinator: RatioCoordinator, serial: str
) -> list[RatioSensor]:
    return [
        RatioSensor(coordinator, serial, desc)
        for desc in (*SENSOR_DESCRIPTIONS, *FIRMWARE_SENSOR_DESCRIPTIONS)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RatioCoordinator = data["coordinator"]
    history: RatioHistoryCoordinator | None = data.get("history_coordinator")
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities: list[CoordinatorEntity] = []
        for serial in new:
            entities.extend(_build_sensor_entities(coordinator, serial))
            if history is not None:
                entities.extend(
                    RatioLastSessionSensor(history, serial, desc)
                    for desc in LAST_SESSION_DESCRIPTIONS
                )
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


class RatioLastSessionSensor(
    CoordinatorEntity[RatioHistoryCoordinator], SensorEntity
):
    """Sensor reading the most-recent completed session for a charger."""

    entity_description: RatioLastSessionSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        history: RatioHistoryCoordinator,
        serial: str,
        description: RatioLastSessionSensorDescription,
    ) -> None:
        super().__init__(history)
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
    def native_value(self) -> Any:
        session = _last_session(self.coordinator, self._serial)
        if session is None:
            return None
        try:
            return self.entity_description.value_fn(session)
        except AttributeError:
            return None
