"""Tests for the ratio.ble_probe diagnostic service.

The probe is a short-lived development aid for the RPA-rotation +
proxy-routing investigation. It bypasses ``coordinator.address`` and looks
up the strongest current advertisement for ``RATIO_<serial>`` via
``async_discovered_service_info``, then attempts a connect+read. The
response surface lets a developer call it from ``hass-cli`` and read the
outcome without parsing HA log scrollback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN, SERVICE_BLE_PROBE


def _make_service_info(
    *,
    name: str,
    address: str,
    rssi: int,
    source: str,
    scanner_class: str = "HaScanner",
) -> MagicMock:
    """Build a fake ``BluetoothServiceInfoBleak`` for ``async_discovered_service_info``."""
    info = MagicMock()
    info.name = name
    info.address = address
    info.rssi = rssi
    info.source = source
    info.device = MagicMock(name=f"BLEDevice({address})")
    info.connectable = True
    scanner = MagicMock()
    scanner.source = source
    type(scanner).__name__ = scanner_class  # so type(scanner).__name__ resolves cleanly
    info.device.scanner = scanner
    return info


@pytest.mark.asyncio
async def test_ble_probe_picks_strongest_rssi_and_reports_success(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """The probe enumerates discovered service info, picks the highest RSSI
    match for ``RATIO_<serial>``, connects via its BLEDevice, and returns a
    structured response naming the chosen scanner."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "P00000000013428")},
    )

    weak = _make_service_info(
        name="RATIO_P00000000013428",
        address="C0:49:EF:F5:AA:FE",
        rssi=-85,
        source="40:23:43:1D:C8:EC",
        scanner_class="HaScanner",
    )
    strong = _make_service_info(
        name="RATIO_P00000000013428",
        address="63:0F:29:56:F2:9C",
        rssi=-72,
        source="F4:2D:C9:70:C2:7E",
        scanner_class="ESPHomeScanner",
    )
    unrelated = _make_service_info(
        name="Fridge",
        address="BC:10:2F:20:DE:13",
        rssi=-60,
        source="40:23:43:1D:C8:EC",
    )

    ble_client = MagicMock()
    ble_client.__aenter__ = AsyncMock(return_value=ble_client)
    ble_client.__aexit__ = AsyncMock(return_value=None)
    ble_client.get_charger_sensor_values = AsyncMock(
        return_value=MagicMock(protocol_version=6)
    )

    with (
        patch(
            "custom_components.ratio.services.async_discovered_service_info",
            return_value=[weak, unrelated, strong],
        ),
        patch(
            "custom_components.ratio.services.BleClient",
            return_value=ble_client,
        ) as ble_client_cls,
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BLE_PROBE,
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )

    assert response is not None
    assert response["status"] == "ok"
    assert response["chosen"]["address"] == "63:0F:29:56:F2:9C"
    assert response["chosen"]["rssi"] == -72
    assert response["chosen"]["scanner_source"] == "F4:2D:C9:70:C2:7E"
    assert response["chosen"]["scanner_class"] == "ESPHomeScanner"
    candidates = response["candidates"]
    assert [c["address"] for c in candidates] == [
        "63:0F:29:56:F2:9C",
        "C0:49:EF:F5:AA:FE",
    ]
    # BleClient was constructed with the strongest device, not the weakest.
    ble_client_cls.assert_called_once_with(strong.device)
    ble_client.get_charger_sensor_values.assert_awaited_once()


@pytest.mark.asyncio
async def test_ble_probe_reports_no_advert_when_charger_invisible(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """When no scanner has seen the local_name, the probe reports
    ``no_advert`` with an empty candidate list — no connect attempted."""
    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "P00000000099999")},
    )
    unrelated = _make_service_info(
        name="Fridge",
        address="BC:10:2F:20:DE:13",
        rssi=-60,
        source="40:23:43:1D:C8:EC",
    )

    with (
        patch(
            "custom_components.ratio.services.async_discovered_service_info",
            return_value=[unrelated],
        ),
        patch("custom_components.ratio.services.BleClient") as ble_client_cls,
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BLE_PROBE,
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )

    assert response is not None
    assert response["status"] == "no_advert"
    assert response["candidates"] == []
    ble_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_ble_probe_surfaces_connect_failure_without_raising(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
) -> None:
    """When the connect or read fails, the probe returns ``status='error'``
    with the exception class name + message — so a developer sees the same
    failure that ``RatioBleCoordinator`` would, but as a service response
    rather than a coordinator UpdateFailed."""
    from aioratio.exceptions import RatioBleConnectionError

    entry = setup_integration
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "P00000000013428")},
    )

    info = _make_service_info(
        name="RATIO_P00000000013428",
        address="63:0F:29:56:F2:9C",
        rssi=-72,
        source="F4:2D:C9:70:C2:7E",
        scanner_class="ESPHomeScanner",
    )

    ble_client = MagicMock()
    ble_client.__aenter__ = AsyncMock(
        side_effect=RatioBleConnectionError(
            "BLE connect failed: ratio-charger - 63:0F:29:56:F2:9C: "
            "Failed to connect after 3 attempt(s): TimeoutError"
        )
    )
    ble_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "custom_components.ratio.services.async_discovered_service_info",
            return_value=[info],
        ),
        patch(
            "custom_components.ratio.services.BleClient",
            return_value=ble_client,
        ),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BLE_PROBE,
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )

    assert response is not None
    assert response["status"] == "error"
    assert response["error_type"] == "RatioBleConnectionError"
    assert "Failed to connect" in response["error"]
    assert response["chosen"]["address"] == "63:0F:29:56:F2:9C"
