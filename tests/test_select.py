"""Tests for Ratio select entities."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aioratio.models import Vehicle

from custom_components.ratio.coordinator import RatioData

# select.py imports DeviceInfo from device_registry, which may not exist in
# older HA stubs shipped with pytest-homeassistant-custom-component. Patch it.
try:
    from homeassistant.helpers.device_registry import DeviceInfo  # noqa: F401
except ImportError:
    import homeassistant.helpers.device_registry as _dr
    from homeassistant.helpers.entity import DeviceInfo as _DI  # type: ignore[attr-defined]

    _dr.DeviceInfo = _DI  # type: ignore[attr-defined]

from custom_components.ratio.select import RatioActiveVehicleSelect


def _make_coordinator(vehicles: list[Vehicle]) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(vehicles=vehicles)
    coord.preferred_vehicle = {}
    return coord


def test_duplicate_vehicle_names_produce_unique_options() -> None:
    """Two vehicles with the same name should get disambiguated options."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="My Car"),
        Vehicle(vehicle_id="v2", vehicle_name="My Car"),
    ]
    coord = _make_coordinator(vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, "SN001")

    opts = entity.options
    assert len(opts) == 2
    assert len(set(opts)) == 2  # all unique
    assert "My Car (v1)" in opts
    assert "My Car (v2)" in opts


def test_unique_vehicle_names_not_disambiguated() -> None:
    """Vehicles with distinct names should not get IDs appended."""
    vehicles = [
        Vehicle(vehicle_id="v1", vehicle_name="Tesla"),
        Vehicle(vehicle_id="v2", vehicle_name="BMW"),
    ]
    coord = _make_coordinator(vehicles)
    client = MagicMock()

    entity = RatioActiveVehicleSelect(coord, client, "SN001")

    opts = entity.options
    assert opts == ["Tesla", "BMW"]
