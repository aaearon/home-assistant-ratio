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
from bleak.exc import BleakError  # noqa: E402
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


def _make_service_info(
    address: str = "AA:BB:CC:DD:EE:FF",
    *,
    name: str | None = "RATIO_SN001",
    rssi: int = -70,
    source: str = "00:11:22:33:44:55",
) -> MagicMock:
    """Build a fake BluetoothServiceInfoBleak.

    Defaults match the canonical test serial ``SN001`` so the coordinator's
    ``RATIO_{serial}`` matcher accepts it.
    """
    info = MagicMock()
    info.address = address
    info.name = name
    info.rssi = rssi
    info.source = source
    info.device = MagicMock(name=f"BLEDevice({address})")
    info.connectable = True
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
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
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


def _make_raw_bleak_client_mock(*, pair_result: object = True) -> MagicMock:
    """Build a ``BleakClient`` mock for the raw-bleak path in ``_try_pair``.

    ``pair_result`` is either a bool (RPC return value) or an Exception
    instance (raised by ``pair()``).
    """
    raw = MagicMock()
    raw.__aenter__ = AsyncMock(return_value=raw)
    raw.__aexit__ = AsyncMock(return_value=None)
    if isinstance(pair_result, Exception):
        raw.pair = AsyncMock(side_effect=pair_result)
    else:
        raw.pair = AsyncMock(return_value=pair_result)
    return raw


@pytest.mark.asyncio
async def test_bond_error_creates_repair_issue(hass: HomeAssistant) -> None:
    """RatioBleNotBondedError + failed pair should create a HA repair issue."""
    ble_client = MagicMock()
    ble_client.__aenter__ = AsyncMock(return_value=ble_client)
    ble_client.__aexit__ = AsyncMock(return_value=None)
    ble_client.get_charger_sensor_values = AsyncMock(
        side_effect=RatioBleNotBondedError("not bonded")
    )
    service_info = _make_service_info()
    ble_device = MagicMock()
    # pair() raises BleakError — the most common bonding failure on BlueZ.
    raw_bleak = _make_raw_bleak_client_mock(
        pair_result=BleakError("Authentication Failed")
    )

    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
        patch("custom_components.ratio.ble.BleakClient", return_value=raw_bleak),
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
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
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
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=ble_client),
    ):
        snapshot = await coord._async_update(service_info)

    assert isinstance(snapshot, BleSnapshot)
    assert cloud_coord.data is sentinel.cloud_data
    cloud_coord.async_set_updated_data.assert_not_called()


@pytest.mark.asyncio
async def test_try_pair_returns_true_when_pair_completes(hass: HomeAssistant) -> None:
    """A successful ``pair()`` (None return on bleak >=1.0) yields True.

    Documents the public contract: in bleak >=1.0 ``BleakClient.pair()`` is
    typed as ``-> None`` and signals failure by raising. The integration must
    treat a clean exit from the ``async with`` block as success.
    """
    ble_device = MagicMock()
    raw_bleak = _make_raw_bleak_client_mock(pair_result=None)
    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleakClient", return_value=raw_bleak),
    ):
        result = await coord._try_pair()

    assert result is True


@pytest.mark.asyncio
async def test_try_pair_logs_firmware_hint_on_not_implemented(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """``NotImplementedError`` from bleak_esphome must log the firmware hint.

    bleak_esphome raises ``NotImplementedError`` from ``pair()`` when the
    proxy lacks the PAIRING feature flag (firmware < 2024.6 or
    ``active: false``). The log must point the user at the firmware fix.
    """
    import logging

    ble_device = MagicMock()
    raw_bleak = _make_raw_bleak_client_mock(
        pair_result=NotImplementedError(
            "Pairing is not available in this version ESPHome"
        )
    )
    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleakClient", return_value=raw_bleak),
        caplog.at_level(logging.WARNING, logger="custom_components.ratio.ble"),
    ):
        result = await coord._try_pair()

    assert result is False
    combined = "\n".join(record.message for record in caplog.records)
    assert "bluetooth_proxy" in combined
    assert "2024.6" in combined


@pytest.mark.asyncio
async def test_try_pair_logs_bleak_error_with_class_name(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A BleakError from ``pair()`` must log the concrete exception class.

    The class name is the diagnostic — bare ``BleakError`` covers a wide
    range of underlying causes (BlueZ DBus, ESPHome API failure, timeout).
    """
    import logging

    ble_device = MagicMock()
    raw_bleak = _make_raw_bleak_client_mock(
        pair_result=BleakError("Authentication Failed (0x05)")
    )
    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch("custom_components.ratio.ble.BleakClient", return_value=raw_bleak),
        caplog.at_level(logging.WARNING, logger="custom_components.ratio.ble"),
    ):
        result = await coord._try_pair()

    assert result is False
    combined = "\n".join(record.message for record in caplog.records)
    assert "BleakError" in combined
    assert "Authentication Failed" in combined


def test_scanner_info_detects_remote_proxy_scanner() -> None:
    """_scanner_info reports proxy=True for a BaseHaRemoteScanner."""
    from homeassistant.components.bluetooth import BaseHaRemoteScanner

    from custom_components.ratio.ble import _scanner_info

    proxy_scanner = MagicMock(spec=BaseHaRemoteScanner)
    proxy_scanner.source = "AA:BB:CC:DD:EE:00"
    scanner_dev = MagicMock()
    scanner_dev.scanner = proxy_scanner

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    # Source format is ``<scanner-class>:<scanner.source>`` so reporters
    # can tell ESPHomeScanner from ShellyScanner without extra log digging.
    assert source is not None
    assert source.endswith(":AA:BB:CC:DD:EE:00")
    assert is_proxy is True


def test_scanner_info_returns_local_scanner_as_non_proxy() -> None:
    """A local (BlueZ) scanner is not a BaseHaRemoteScanner, so proxy=False."""
    from custom_components.ratio.ble import _scanner_info

    local_scanner = MagicMock()
    local_scanner.source = "hci0"
    scanner_dev = MagicMock()
    scanner_dev.scanner = local_scanner

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is not None
    assert source.endswith(":hci0")
    assert is_proxy is False


def test_scanner_info_handles_no_scanner_for_address() -> None:
    """If no scanner is currently providing the device, return (None, False)."""
    from custom_components.ratio.ble import _scanner_info

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is None
    assert is_proxy is False


def test_scanner_info_swallows_api_errors() -> None:
    """The helper is diagnostic and must never raise — even on API errors."""
    from custom_components.ratio.ble import _scanner_info

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        side_effect=RuntimeError("bt manager not ready"),
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is None
    assert is_proxy is False


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


# ---------------------------------------------------------------------------
# _pick_best_device — name-based scanner selection.
#
# Reason for the refactor: the integration originally locked
# ``coordinator.address`` to the address discovered at config-entry creation
# time. Ratio chargers rotate Resolvable Private Addresses every ~minute, so
# an ESPHome BT-proxy (which sees only RPAs) and the local BlueZ adapter
# (which observes the static random identity address) report the same
# charger under different MACs. Looking up by ``self.address`` always
# resolved to whichever scanner happened to see that one address — usually
# the weaker one. ``_pick_best_device`` walks every connectable advert,
# filters by ``RATIO_<serial>`` (stable across rotation), and picks the
# strongest RSSI so the coordinator routes through whichever scanner is
# actually closest to the charger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_best_device_returns_strongest_rssi(hass: HomeAssistant) -> None:
    """When multiple scanners see the charger, pick the strongest-RSSI advert."""
    weak = _make_service_info(
        address="C0:49:EF:F5:AA:FE", rssi=-86, source="40:23:43:1D:C8:EC"
    )
    strong = _make_service_info(
        address="79:75:75:A4:A0:45", rssi=-59, source="F4:2D:C9:70:C2:7E"
    )
    unrelated = _make_service_info(
        address="BC:10:2F:20:DE:13", name="Fridge", rssi=-50
    )
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[weak, unrelated, strong],
    ):
        device = coord._pick_best_device()

    assert device is strong.device
    # self.address is updated so diagnostics + _scanner_info reflect the
    # scanner actually being used.
    assert coord.address == "79:75:75:A4:A0:45"


@pytest.mark.asyncio
async def test_pick_best_device_returns_none_when_no_match(
    hass: HomeAssistant,
) -> None:
    """If no advert matches ``RATIO_<serial>``, return None — caller raises."""
    unrelated = _make_service_info(
        address="BC:10:2F:20:DE:13", name="Fridge", rssi=-50
    )
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[unrelated],
    ):
        device = coord._pick_best_device()

    assert device is None


@pytest.mark.asyncio
async def test_async_update_uses_strongest_scanner(hass: HomeAssistant) -> None:
    """_async_update routes the BleClient via the strongest-RSSI scanner."""
    from custom_components.ratio.ble import BleSnapshot

    sensor_resp = _make_sensor_response()
    ble_client = _make_ble_client_mock(sensor_resp)

    weak = _make_service_info(
        address="C0:49:EF:F5:AA:FE", rssi=-86, source="40:23:43:1D:C8:EC"
    )
    strong = _make_service_info(
        address="79:75:75:A4:A0:45", rssi=-59, source="F4:2D:C9:70:C2:7E"
    )

    coord = _make_coordinator(hass)

    with (
        patch(
            "custom_components.ratio.ble.async_discovered_service_info",
            return_value=[weak, strong],
        ),
        patch(
            "custom_components.ratio.ble.BleClient", return_value=ble_client
        ) as ble_client_cls,
    ):
        snapshot = await coord._async_update(_make_service_info())

    assert isinstance(snapshot, BleSnapshot)
    ble_client_cls.assert_called_once_with(strong.device)


@pytest.mark.asyncio
async def test_async_update_raises_when_no_advert(hass: HomeAssistant) -> None:
    """No advert for the local_name → UpdateFailed, no connect attempt."""
    coord = _make_coordinator(hass)
    with (
        patch(
            "custom_components.ratio.ble.async_discovered_service_info",
            return_value=[],
        ),
        patch("custom_components.ratio.ble.BleClient") as ble_client_cls,
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update(_make_service_info())

    ble_client_cls.assert_not_called()
