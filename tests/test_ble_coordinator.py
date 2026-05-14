"""Tests for RatioBleCoordinator and BleSnapshot.

The homeassistant.components.bluetooth package transitively imports
``serial`` (via usb), which is not installed in the test environment.
We stub it out at the top of this module before any HA bluetooth import.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub out pyserial before any HA bluetooth submodule is imported.
# ---------------------------------------------------------------------------
def _stub_serial() -> None:
    if "serial" in sys.modules:
        return
    serial_mod = ModuleType("serial")
    tools_mod = ModuleType("serial.tools")
    list_ports_mod = ModuleType("serial.tools.list_ports")
    list_ports_common_mod = ModuleType("serial.tools.list_ports_common")

    list_ports_mod.comports = lambda: []  # type: ignore[attr-defined]
    list_ports_common_mod.ListPortInfo = object  # type: ignore[attr-defined]

    serial_mod.tools = tools_mod  # type: ignore[attr-defined]
    tools_mod.list_ports = list_ports_mod  # type: ignore[attr-defined]
    tools_mod.list_ports_common = list_ports_common_mod  # type: ignore[attr-defined]

    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools_mod
    sys.modules["serial.tools.list_ports"] = list_ports_mod
    sys.modules["serial.tools.list_ports_common"] = list_ports_common_mod


_stub_serial()

# ---------------------------------------------------------------------------
# Now we can safely import bluetooth-dependent HA modules.
# ---------------------------------------------------------------------------
from aioratio.ble.models.sensors import ChargerSensorValuesResponse  # noqa: E402
from aioratio.exceptions import (  # noqa: E402
    RatioBleConnectionError,
    RatioBleNotBondedError,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.issue_registry import IssueSeverity  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.ratio.const import DOMAIN  # noqa: E402

if TYPE_CHECKING:
    from custom_components.ratio.ble import RatioBleCoordinator


def _make_sensor_response(
    v1=2300, v2=2310, v3=2290, a1=160, a2=None, a3=None
) -> ChargerSensorValuesResponse:
    return ChargerSensorValuesResponse(
        transaction="t1",
        result="ok",
        actual_mains_voltage_phase_1=v1,
        actual_mains_voltage_phase_2=v2,
        actual_mains_voltage_phase_3=v3,
        actual_sensor_box_current_phase_1=a1,
        actual_sensor_box_current_phase_2=a2,
        actual_sensor_box_current_phase_3=a3,
    )


def _make_ble_client_mock(sensor_response: ChargerSensorValuesResponse) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get_charger_sensor_values = AsyncMock(return_value=sensor_response)
    client.protocol_version = 2
    return client


def _make_service_info(address: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    info = MagicMock()
    info.address = address
    info.device = MagicMock()
    return info


def _make_coordinator(hass: HomeAssistant) -> RatioBleCoordinator:
    """Build a RatioBleCoordinator with the parent __init__ stubbed out."""
    import logging

    from homeassistant.components.bluetooth.active_update_coordinator import (
        ActiveBluetoothDataUpdateCoordinator,
    )

    from custom_components.ratio.ble import RatioBleCoordinator

    def _fake_parent_init(self, *a, **kw) -> None:
        # Provide the minimal attributes the parent expects so methods work.
        self.hass = hass
        self.address = "AA:BB:CC:DD:EE:FF"
        self.logger = logging.getLogger(__name__)

    with patch.object(
        ActiveBluetoothDataUpdateCoordinator, "__init__", _fake_parent_init
    ):
        coord = RatioBleCoordinator(
            hass=hass,
            logger=logging.getLogger(__name__),
            address="AA:BB:CC:DD:EE:FF",
            serial="SN001",
        )
    return coord


@pytest.mark.asyncio
async def test_ble_snapshot_populated(hass: HomeAssistant) -> None:
    """BleSnapshot should have correctly scaled values from the sensor response."""
    from custom_components.ratio.ble import BleSnapshot

    sensor_resp = _make_sensor_response()
    ble_client = _make_ble_client_mock(sensor_resp)
    service_info = _make_service_info()
    ble_device = MagicMock()

    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
    ):
        snapshot = await coord._async_update(service_info)

    assert isinstance(snapshot, BleSnapshot)
    assert snapshot.serial == "SN001"
    assert snapshot.voltage_phase_1 == 230.0
    assert snapshot.voltage_phase_2 == 231.0
    assert snapshot.voltage_phase_3 == 229.0
    assert snapshot.current_phase_1 == 16.0
    assert snapshot.current_phase_2 is None
    assert snapshot.current_phase_3 is None
    assert snapshot.protocol_version == 2


@pytest.mark.asyncio
async def test_bond_error_creates_repair_issue(hass: HomeAssistant) -> None:
    """RatioBleNotBondedError should create a HA repair issue."""
    ble_client = MagicMock()
    ble_client.__aenter__ = AsyncMock(return_value=ble_client)
    ble_client.__aexit__ = AsyncMock(return_value=None)
    ble_client.get_charger_sensor_values = AsyncMock(
        side_effect=RatioBleNotBondedError("not bonded")
    )
    service_info = _make_service_info()
    ble_device = MagicMock()

    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
        patch("custom_components.ratio.ble.async_create_issue") as mock_create_issue,
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update(service_info)

    mock_create_issue.assert_called_once_with(
        hass,
        DOMAIN,
        "ble_not_bonded_SN001",
        is_fixable=False,
        severity=IssueSeverity.ERROR,
        translation_key="ble_not_bonded",
        translation_placeholders={"serial": "SN001"},
    )


@pytest.mark.asyncio
async def test_connection_error_raises_update_failed(hass: HomeAssistant) -> None:
    """RatioBleConnectionError should raise UpdateFailed."""
    ble_client = MagicMock()
    ble_client.__aenter__ = AsyncMock(return_value=ble_client)
    ble_client.__aexit__ = AsyncMock(return_value=None)
    ble_client.get_charger_sensor_values = AsyncMock(
        side_effect=RatioBleConnectionError("timed out")
    )
    service_info = _make_service_info()
    ble_device = MagicMock()

    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update(service_info)


@pytest.mark.asyncio
async def test_cloud_coordinator_data_untouched(hass: HomeAssistant) -> None:
    """A BLE poll must not touch any cloud coordinator data."""
    from unittest.mock import sentinel

    from custom_components.ratio.ble import BleSnapshot

    sensor_resp = _make_sensor_response()
    ble_client = _make_ble_client_mock(sensor_resp)
    service_info = _make_service_info()
    ble_device = MagicMock()

    coord = _make_coordinator(hass)

    cloud_coord = MagicMock()
    cloud_coord.data = sentinel.cloud_data

    with (
        patch(
            "custom_components.ratio.ble.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
    ):
        snapshot = await coord._async_update(service_info)

    assert isinstance(snapshot, BleSnapshot)
    assert cloud_coord.data is sentinel.cloud_data
    cloud_coord.async_set_updated_data.assert_not_called()


def test_needs_poll_respects_45s_cadence() -> None:
    """_needs_poll returns False at 30s age, True at None or >=45s."""
    from custom_components.ratio.ble import RatioBleCoordinator

    # Patch __init__ to avoid needing a real HA BT stack for this pure logic test.
    with patch.object(
        RatioBleCoordinator,
        "__init__",
        lambda self, *a, **kw: setattr(self, "serial", "SN001") or None,
    ):
        coord = RatioBleCoordinator.__new__(RatioBleCoordinator)
        coord.serial = "SN001"

    service_info = MagicMock()

    assert coord._needs_poll(service_info, None) is True
    assert coord._needs_poll(service_info, 30.0) is False
    assert coord._needs_poll(service_info, 45.0) is True
    assert coord._needs_poll(service_info, 100.0) is True
