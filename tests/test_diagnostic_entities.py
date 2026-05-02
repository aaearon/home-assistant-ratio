"""Tests for diagnostic sensor and binary sensor entities."""

from __future__ import annotations

from unittest.mock import MagicMock

from aioratio.models import ChargerOverview, InstallerOcppSettings
from aioratio.models.diagnostics import (
    BackendStatus,
    ChargerDiagnostics,
    ConnectivityController,
    EthernetStatus,
    Ipv4,
    MainController,
    NetworkStatus,
    OcppDiagnosticStatus,
    ProductInformation,
    WifiStatus,
)

from custom_components.ratio.binary_sensor import (
    DIAGNOSTIC_BINARY_SENSOR_DESCRIPTIONS,
    RatioDiagnosticBinarySensor,
)
from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.sensor import (
    DIAGNOSTIC_SENSOR_DESCRIPTIONS,
    OCPP_SENSOR_DESCRIPTIONS,
    RatioDiagnosticSensor,
    RatioOcppSensor,
)

SERIAL = "SN001"


def _make_full_diag() -> ChargerDiagnostics:
    return ChargerDiagnostics(
        product_information=ProductInformation(
            main_controller=MainController(
                serial_number="CPC-001",
                hardware_type="CPC-V2",
                hardware_version="HW-2",
                firmware_version="4.0.0",
            ),
            connectivity_controller=ConnectivityController(
                firmware_version="1.2.0",
                hardware_version="CC-HW1",
            ),
        ),
        network_status=NetworkStatus(
            is_time_synchronized=True,
            connection_medium="WIFI",
            wifi=WifiStatus(
                connected=True,
                ssid="HomeNet",
                rssi=-55,
                ipv4=Ipv4(
                    address="192.168.1.50",
                    netmask="255.255.255.0",
                    gateway="192.168.1.1",
                ),
            ),
            ethernet=EthernetStatus(connected=False, ipv4=None),
        ),
        backend_status=BackendStatus(connected=True),
        ocpp_status=OcppDiagnosticStatus(
            connected=True,
            enabled=True,
            cpms_name="Operator A",
            cpms_url="ws://op.example.com",
        ),
    )


def _coord_with_diag(
    diag: ChargerDiagnostics, ocpp: InstallerOcppSettings | None = None
) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(
        chargers={SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})},
        diagnostics={SERIAL: diag},
        ocpp_settings={SERIAL: ocpp or InstallerOcppSettings()},
    )
    coord.last_update_success = True
    return coord


def _diag_sensor(coord: MagicMock, key: str) -> RatioDiagnosticSensor:
    desc = next(d for d in DIAGNOSTIC_SENSOR_DESCRIPTIONS if d.key == key)
    return RatioDiagnosticSensor(coord, SERIAL, desc)


def _diag_binary(coord: MagicMock, key: str) -> RatioDiagnosticBinarySensor:
    desc = next(d for d in DIAGNOSTIC_BINARY_SENSOR_DESCRIPTIONS if d.key == key)
    return RatioDiagnosticBinarySensor(coord, SERIAL, desc)


def _ocpp_sensor(coord: MagicMock, key: str) -> RatioOcppSensor:
    desc = next(d for d in OCPP_SENSOR_DESCRIPTIONS if d.key == key)
    return RatioOcppSensor(coord, SERIAL, desc)


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------


def test_cpc_serial_number_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "cpc_serial_number").native_value == "CPC-001"


def test_hardware_type_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "hardware_type").native_value == "CPC-V2"


def test_firmware_version_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "firmware_version").native_value == "4.0.0"


def test_hardware_version_sensor_disabled_by_default() -> None:
    desc = next(
        d for d in DIAGNOSTIC_SENSOR_DESCRIPTIONS if d.key == "hardware_version"
    )
    assert desc.entity_registry_enabled_default is False


def test_connectivity_firmware_version_sensor_disabled_by_default() -> None:
    desc = next(
        d
        for d in DIAGNOSTIC_SENSOR_DESCRIPTIONS
        if d.key == "connectivity_firmware_version"
    )
    assert desc.entity_registry_enabled_default is False


def test_wifi_ssid_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "wifi_ssid").native_value == "HomeNet"


def test_wifi_rssi_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "wifi_rssi").native_value == -55


def test_connection_medium_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "connection_medium").native_value == "WIFI"


def test_cpms_name_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "cpms_name").native_value == "Operator A"


def test_cpms_url_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_sensor(coord, "cpms_url").native_value == "ws://op.example.com"


def test_sensor_returns_none_when_no_diagnostics() -> None:
    coord = MagicMock()
    coord.data = RatioData(
        chargers={SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})}
    )
    assert _diag_sensor(coord, "cpc_serial_number").native_value is None


def test_sensor_resilient_to_missing_wifi() -> None:
    """wifi_ssid sensor returns None when wifi is None."""
    diag = ChargerDiagnostics(
        network_status=NetworkStatus(connection_medium="ETHERNET")
    )
    coord = _coord_with_diag(diag)
    assert _diag_sensor(coord, "wifi_ssid").native_value is None
    assert _diag_sensor(coord, "wifi_rssi").native_value is None


def test_sensor_resilient_to_missing_main_controller() -> None:
    diag = ChargerDiagnostics(product_information=ProductInformation())
    coord = _coord_with_diag(diag)
    assert _diag_sensor(coord, "cpc_serial_number").native_value is None
    assert _diag_sensor(coord, "firmware_version").native_value is None


# ---------------------------------------------------------------------------
# OCPP sensor
# ---------------------------------------------------------------------------


def test_charge_point_identifier_sensor() -> None:
    coord = _coord_with_diag(
        ChargerDiagnostics(),
        ocpp=InstallerOcppSettings(charge_point_identifier="CP-001"),
    )
    assert _ocpp_sensor(coord, "charge_point_identifier").native_value == "CP-001"


def test_charge_point_identifier_sensor_none_when_no_ocpp() -> None:
    coord = MagicMock()
    coord.data = RatioData(
        chargers={SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})}
    )
    assert _ocpp_sensor(coord, "charge_point_identifier").native_value is None


# ---------------------------------------------------------------------------
# Diagnostic binary sensors
# ---------------------------------------------------------------------------


def test_backend_connected_binary_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_binary(coord, "backend_connected").is_on is True


def test_wifi_connected_binary_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_binary(coord, "wifi_connected").is_on is True


def test_ethernet_connected_binary_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_binary(coord, "ethernet_connected").is_on is False


def test_ocpp_connected_binary_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_binary(coord, "ocpp_connected").is_on is True


def test_time_synchronized_binary_sensor() -> None:
    coord = _coord_with_diag(_make_full_diag())
    assert _diag_binary(coord, "time_synchronized").is_on is True


def test_binary_sensor_returns_none_when_no_diagnostics() -> None:
    coord = MagicMock()
    coord.data = RatioData(
        chargers={SERIAL: ChargerOverview.from_dict({"serialNumber": SERIAL})}
    )
    assert _diag_binary(coord, "backend_connected").is_on is None


def test_binary_sensor_resilient_to_missing_network_status() -> None:
    """All network binary sensors return None when network_status is None."""
    diag = ChargerDiagnostics(backend_status=BackendStatus(connected=True))
    coord = _coord_with_diag(diag)
    assert _diag_binary(coord, "wifi_connected").is_on is None
    assert _diag_binary(coord, "ethernet_connected").is_on is None
    assert _diag_binary(coord, "time_synchronized").is_on is None
    assert _diag_binary(coord, "backend_connected").is_on is True
