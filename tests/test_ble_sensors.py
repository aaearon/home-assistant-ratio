"""Tests for BLE-only sensor entities (task 5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ratio.ble import BleSnapshot
from custom_components.ratio.sensor import (
    BLE_SENSOR_DESCRIPTIONS,
    RatioBleSensor,
    RatioBleSensorEntityDescription,
)


def _make_ble_coordinator(serial: str = "S1", data: BleSnapshot | None = None) -> MagicMock:
    """Build a minimal mock RatioBleCoordinator."""
    coord = MagicMock()
    coord.serial = serial
    coord.data = data
    coord.last_update_success = True
    return coord


def _make_snapshot(
    serial: str = "S1",
    voltage_phase_1: float | None = 230.5,
    voltage_phase_2: float | None = 231.0,
    voltage_phase_3: float | None = 229.8,
    current_phase_1: float | None = 16.0,
    current_phase_2: float | None = None,
    current_phase_3: float | None = None,
    protocol_version: int | None = 2,
) -> BleSnapshot:
    return BleSnapshot(
        serial=serial,
        voltage_phase_1=voltage_phase_1,
        voltage_phase_2=voltage_phase_2,
        voltage_phase_3=voltage_phase_3,
        current_phase_1=current_phase_1,
        current_phase_2=current_phase_2,
        current_phase_3=current_phase_3,
        protocol_version=protocol_version,
    )


# ---------------------------------------------------------------------------
# Description-level tests
# ---------------------------------------------------------------------------


def test_ble_sensor_descriptions_count() -> None:
    """Exactly 7 BLE sensor descriptions must be defined."""
    assert len(BLE_SENSOR_DESCRIPTIONS) == 7


def test_ble_sensor_description_keys() -> None:
    expected_keys = {
        "voltage_phase_1",
        "voltage_phase_2",
        "voltage_phase_3",
        "current_phase_1",
        "current_phase_2",
        "current_phase_3",
        "ble_protocol_version",
    }
    assert {d.key for d in BLE_SENSOR_DESCRIPTIONS} == expected_keys


def test_ble_sensor_description_type() -> None:
    for desc in BLE_SENSOR_DESCRIPTIONS:
        assert isinstance(desc, RatioBleSensorEntityDescription)


# ---------------------------------------------------------------------------
# Entity-level tests
# ---------------------------------------------------------------------------


def test_ble_sensor_native_value() -> None:
    """native_value returns the correct field from the BleSnapshot."""
    snap = _make_snapshot(voltage_phase_1=230.5)
    coord = _make_ble_coordinator(data=snap)
    desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == "voltage_phase_1")
    entity = RatioBleSensor(coord, desc)
    assert entity.native_value == 230.5


def test_ble_sensor_unavailable_when_data_none() -> None:
    """native_value returns None when coordinator.data is None."""
    coord = _make_ble_coordinator(data=None)
    desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == "voltage_phase_1")
    entity = RatioBleSensor(coord, desc)
    assert entity.native_value is None


def test_ble_sensor_protocol_version() -> None:
    snap = _make_snapshot(protocol_version=3)
    coord = _make_ble_coordinator(data=snap)
    desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == "ble_protocol_version")
    entity = RatioBleSensor(coord, desc)
    assert entity.native_value == 3


def test_ble_sensor_current_none_when_single_phase() -> None:
    snap = _make_snapshot(current_phase_2=None, current_phase_3=None)
    coord = _make_ble_coordinator(data=snap)
    for key in ("current_phase_2", "current_phase_3"):
        desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == key)
        entity = RatioBleSensor(coord, desc)
        assert entity.native_value is None


def test_ble_sensor_unique_id() -> None:
    coord = _make_ble_coordinator(serial="MYSERIAL")
    desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == "voltage_phase_1")
    entity = RatioBleSensor(coord, desc)
    assert entity.unique_id == "MYSERIAL_voltage_phase_1"


def test_ble_sensor_has_entity_name() -> None:
    coord = _make_ble_coordinator()
    desc = next(d for d in BLE_SENSOR_DESCRIPTIONS if d.key == "voltage_phase_1")
    entity = RatioBleSensor(coord, desc)
    assert entity._attr_has_entity_name is True


# ---------------------------------------------------------------------------
# async_setup_entry integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ble_sensors_created_when_ble_enabled(hass) -> None:
    """7 BLE sensor entities registered when ble_coordinators has one serial."""
    from custom_components.ratio.sensor import async_setup_entry

    ble_coord = _make_ble_coordinator(serial="S1")

    runtime_data = MagicMock()
    # Cloud coordinator: return early from _add_new by having no data.
    cloud_coord = MagicMock()
    cloud_coord.data = None
    cloud_coord.async_add_listener = MagicMock(return_value=lambda: None)
    runtime_data.coordinator = cloud_coord
    runtime_data.history_coordinator = None
    runtime_data.ble_coordinators = {"S1": ble_coord}

    entry = MagicMock()
    entry.runtime_data = runtime_data
    entry.async_on_unload = MagicMock()

    added_entities: list = []

    def _add_entities(entities, *args, **kwargs) -> None:
        added_entities.extend(entities)

    await async_setup_entry(hass, entry, _add_entities)

    ble_entity_unique_ids = [
        e.unique_id for e in added_entities if isinstance(e, RatioBleSensor)
    ]
    assert len(ble_entity_unique_ids) == 7


@pytest.mark.asyncio
async def test_ble_sensors_not_created_when_ble_disabled(hass) -> None:
    """No BLE entities registered when ble_coordinators is empty."""
    from custom_components.ratio.sensor import async_setup_entry

    runtime_data = MagicMock()
    cloud_coord = MagicMock()
    cloud_coord.data = None
    cloud_coord.async_add_listener = MagicMock(return_value=lambda: None)
    runtime_data.coordinator = cloud_coord
    runtime_data.history_coordinator = None
    runtime_data.ble_coordinators = {}

    entry = MagicMock()
    entry.runtime_data = runtime_data
    entry.async_on_unload = MagicMock()

    added_entities: list = []

    def _add_entities(entities, *args, **kwargs) -> None:
        added_entities.extend(entities)

    await async_setup_entry(hass, entry, _add_entities)

    ble_entities = [e for e in added_entities if isinstance(e, RatioBleSensor)]
    assert len(ble_entities) == 0
