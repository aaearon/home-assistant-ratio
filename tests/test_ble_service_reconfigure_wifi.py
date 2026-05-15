"""Tests for the ratio.reconfigure_wifi BLE service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.ble.models.wifi import WifiAccessPoint
from aioratio.exceptions import RatioBleConnectionError, RatioBleError
from bleak.exc import BleakError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN, SERVICE_RECONFIGURE_WIFI


def _make_wifi_ap(ssid: str) -> MagicMock:
    ap = MagicMock(spec=WifiAccessPoint)
    ap.ssid = ssid
    return ap


def _make_ble_coordinator(
    serial: str = "SN001", address: str = "AA:BB:CC:DD:EE:FF"
) -> MagicMock:
    coord = MagicMock()
    coord.serial = serial
    coord.address = address
    coord._wifi_lock = AsyncMock()
    coord._wifi_lock.__aenter__ = AsyncMock(return_value=None)
    coord._wifi_lock.__aexit__ = AsyncMock(return_value=None)
    return coord


def _make_ble_client_mock(scan_result: list, connect_side_effect=None) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.wifi_scan = AsyncMock(return_value=scan_result)
    if connect_side_effect is not None:
        client.wifi_connect = AsyncMock(side_effect=connect_side_effect)
    else:
        client.wifi_connect = AsyncMock(return_value=MagicMock())
    return client


@pytest.mark.asyncio
async def test_reconfigure_wifi_success(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """Success path: SSID found, wifi_connect called with correct args."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN001")},
    )

    ble_coord = _make_ble_coordinator("SN001")
    ble_client = _make_ble_client_mock(
        scan_result=[_make_wifi_ap("MyNetwork"), _make_wifi_ap("OtherNet")]
    )

    # Inject ble_coordinators into runtime_data
    entry.runtime_data.ble_coordinators = {"SN001": ble_coord}

    ble_device = MagicMock()

    ble_coord._pick_best_device = MagicMock(return_value=ble_device)

    with patch(
        "custom_components.ratio.services.BleClient", return_value=ble_client
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "MyNetwork", "password": "secret"},
            blocking=True,
        )

    ble_client.wifi_connect.assert_awaited_once_with("MyNetwork", "secret")


@pytest.mark.asyncio
async def test_reconfigure_wifi_no_password(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """When password is omitted, wifi_connect is called with None (open network)."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN002")},
    )

    ble_coord = _make_ble_coordinator("SN002")
    ble_client = _make_ble_client_mock(scan_result=[_make_wifi_ap("OpenNet")])
    entry.runtime_data.ble_coordinators = {"SN002": ble_coord}

    ble_device = MagicMock()

    ble_coord._pick_best_device = MagicMock(return_value=ble_device)

    with patch(
        "custom_components.ratio.services.BleClient", return_value=ble_client
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "OpenNet"},
            blocking=True,
        )

    ble_client.wifi_connect.assert_awaited_once_with("OpenNet", None)


@pytest.mark.asyncio
async def test_reconfigure_wifi_ssid_not_found(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """SSID not in scan results raises ServiceValidationError(ssid_not_found)."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN003")},
    )

    ble_coord = _make_ble_coordinator("SN003")
    ble_client = _make_ble_client_mock(scan_result=[_make_wifi_ap("SomeOtherNet")])
    entry.runtime_data.ble_coordinators = {"SN003": ble_coord}

    ble_device = MagicMock()
    ble_coord._pick_best_device = MagicMock(return_value=ble_device)

    with (
        patch("custom_components.ratio.services.BleClient", return_value=ble_client),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "Missing", "password": "pw"},
            blocking=True,
        )

    assert exc_info.value.translation_key == "ssid_not_found"
    ble_client.wifi_connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_wifi_ble_not_enabled(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """No BLE coordinator for the charger raises ServiceValidationError(ble_not_enabled)."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN004")},
    )

    # No ble_coordinators attribute at all (simulating task-4 not yet run)
    if hasattr(entry.runtime_data, "ble_coordinators"):
        del entry.runtime_data.ble_coordinators

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "SomeNet", "password": "pw"},
            blocking=True,
        )

    assert exc_info.value.translation_key == "ble_not_enabled"


@pytest.mark.asyncio
async def test_reconfigure_wifi_ble_not_enabled_empty_dict(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """Serial missing from ble_coordinators dict raises ServiceValidationError(ble_not_enabled)."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN005")},
    )

    # Attribute exists but serial is not in it
    entry.runtime_data.ble_coordinators = {}

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "SomeNet"},
            blocking=True,
        )

    assert exc_info.value.translation_key == "ble_not_enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        RatioBleConnectionError("timeout"),
        RatioBleError("protocol error"),
        BleakError("bleak failure"),
    ],
    ids=["RatioBleConnectionError", "RatioBleError", "BleakError"],
)
async def test_reconfigure_wifi_connect_failed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    exc: Exception,
) -> None:
    """BLE errors from wifi_connect surface as ServiceValidationError(ble_connect_failed)."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN006")},
    )

    ble_coord = _make_ble_coordinator("SN006")
    ble_client = _make_ble_client_mock(
        scan_result=[_make_wifi_ap("TargetNet")],
        connect_side_effect=exc,
    )
    entry.runtime_data.ble_coordinators = {"SN006": ble_coord}

    ble_device = MagicMock()
    ble_coord._pick_best_device = MagicMock(return_value=ble_device)

    with (
        patch("custom_components.ratio.services.BleClient", return_value=ble_client),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONFIGURE_WIFI,
            {"device_id": device.id, "ssid": "TargetNet", "password": "pw"},
            blocking=True,
        )

    assert exc_info.value.translation_key == "ble_connect_failed"
