"""Sensor platform for Ratio EV Charging."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aioratio.models import ChargerOverview
from aioratio.models.diagnostics import ChargerDiagnostics
from aioratio.models.history import Session
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RatioConfigEntry
from .const import DOMAIN
from .coordinator import RatioCoordinator, RatioHistoryCoordinator

PARALLEL_UPDATES = 0


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
            if ov.charger_status is not None
            and ov.charger_status.indicators is not None
            else None
        ),
    ),
)


def _fw(ov: ChargerOverview) -> Any:
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


@dataclass(frozen=True, kw_only=True)
class RatioDiagnosticSensorDescription(SensorEntityDescription):
    """Describes a Ratio diagnostic sensor backed by ChargerDiagnostics."""

    value_fn: Callable[[ChargerDiagnostics], Any] = field(default=lambda _: None)


def _pi(d: ChargerDiagnostics) -> Any:
    return d.product_information


def _mc(d: ChargerDiagnostics) -> Any:
    pi = d.product_information
    return pi.main_controller if pi is not None else None


def _cc(d: ChargerDiagnostics) -> Any:
    pi = d.product_information
    return pi.connectivity_controller if pi is not None else None


def _ns(d: ChargerDiagnostics) -> Any:
    return d.network_status


DIAGNOSTIC_SENSOR_DESCRIPTIONS: tuple[RatioDiagnosticSensorDescription, ...] = (
    RatioDiagnosticSensorDescription(
        key="cpc_serial_number",
        translation_key="cpc_serial_number",
        name="CPC serial number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _mc(d).serial_number if _mc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="connectivity_serial_number",
        translation_key="connectivity_serial_number",
        name="Connectivity serial number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: _cc(d).serial_number if _cc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="hardware_type",
        translation_key="hardware_type",
        name="Hardware type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _mc(d).hardware_type if _mc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="hardware_version",
        translation_key="hardware_version",
        name="Hardware version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: _mc(d).hardware_version if _mc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="Firmware version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _mc(d).firmware_version if _mc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="connectivity_firmware_version",
        translation_key="connectivity_firmware_version",
        name="Connectivity firmware version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: _cc(d).firmware_version if _cc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="connectivity_hardware_version",
        translation_key="connectivity_hardware_version",
        name="Connectivity hardware version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: _cc(d).hardware_version if _cc(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        name="WiFi SSID",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _ns(d).wifi.ssid if _ns(d) and _ns(d).wifi else None,
    ),
    RatioDiagnosticSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        name="WiFi signal strength",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _ns(d).wifi.rssi if _ns(d) and _ns(d).wifi else None,
    ),
    RatioDiagnosticSensorDescription(
        key="wifi_ip",
        translation_key="wifi_ip",
        name="WiFi IP address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (
            _ns(d).wifi.ipv4.address
            if _ns(d) and _ns(d).wifi and _ns(d).wifi.ipv4
            else None
        ),
    ),
    RatioDiagnosticSensorDescription(
        key="ethernet_ip",
        translation_key="ethernet_ip",
        name="Ethernet IP address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (
            _ns(d).ethernet.ipv4.address
            if _ns(d) and _ns(d).ethernet and _ns(d).ethernet.ipv4
            else None
        ),
    ),
    RatioDiagnosticSensorDescription(
        key="connection_medium",
        translation_key="connection_medium",
        name="Connection medium",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _ns(d).connection_medium if _ns(d) else None,
    ),
    RatioDiagnosticSensorDescription(
        key="cpms_name",
        translation_key="cpms_name",
        name="CPMS name",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.ocpp_status.cpms_name if d.ocpp_status else None,
    ),
    RatioDiagnosticSensorDescription(
        key="cpms_url",
        translation_key="cpms_url",
        name="CPMS URL",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.ocpp_status.cpms_url if d.ocpp_status else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class RatioOcppSensorDescription(SensorEntityDescription):
    """Describes a Ratio OCPP settings sensor."""

    value_fn: Callable[[Any], Any] = field(default=lambda _: None)


OCPP_SENSOR_DESCRIPTIONS: tuple[RatioOcppSensorDescription, ...] = (
    RatioOcppSensorDescription(
        key="charge_point_identifier",
        translation_key="charge_point_identifier",
        name="Charge point identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.charge_point_identifier if s else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class RatioLastSessionSensorDescription(SensorEntityDescription):
    """Describes a last-session sensor backed by the history coordinator."""

    value_fn: Callable[[Session], Any] = field(default=lambda _: None)


def _last_session(history: RatioHistoryCoordinator, serial: str) -> Session | None:
    data: dict[str, list[Session]] | None = getattr(history, "data", None)
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
    return datetime.fromtimestamp(int(epoch), tz=UTC)


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
        value_fn=lambda s: s.vehicle.vehicle_name if s.vehicle else None,
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
    entry: RatioConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    history: RatioHistoryCoordinator | None = entry.runtime_data.history_coordinator
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities: list[SensorEntity] = []
        for serial in new:
            entities.extend(_build_sensor_entities(coordinator, serial))
            entities.extend(
                RatioDiagnosticSensor(coordinator, serial, desc)
                for desc in DIAGNOSTIC_SENSOR_DESCRIPTIONS
            )
            entities.extend(
                RatioOcppSensor(coordinator, serial, desc)
                for desc in OCPP_SENSOR_DESCRIPTIONS
            )
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


class RatioDiagnosticSensor(CoordinatorEntity[RatioCoordinator], SensorEntity):
    """Sensor reading diagnostics data for a charger."""

    entity_description: RatioDiagnosticSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RatioCoordinator,
        serial: str,
        description: RatioDiagnosticSensorDescription,
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
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        diag = self.coordinator.data.diagnostics.get(self._serial)
        if diag is None:
            return None
        try:
            return self.entity_description.value_fn(diag)
        except AttributeError:
            return None


class RatioOcppSensor(CoordinatorEntity[RatioCoordinator], SensorEntity):
    """Sensor reading OCPP settings for a charger."""

    entity_description: RatioOcppSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RatioCoordinator,
        serial: str,
        description: RatioOcppSensorDescription,
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
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        settings = self.coordinator.data.ocpp_settings.get(self._serial)
        try:
            return self.entity_description.value_fn(settings)
        except AttributeError:
            return None


class RatioLastSessionSensor(CoordinatorEntity[RatioHistoryCoordinator], SensorEntity):
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
