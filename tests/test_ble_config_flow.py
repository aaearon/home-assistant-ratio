"""Tests for the BLE Bluetooth config flow steps."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import CONF_BLE_ENABLED_SERIALS, DOMAIN


def _make_service_info(name: str, address: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    """Build a minimal mock BluetoothServiceInfoBleak."""
    info = MagicMock()
    info.name = name
    info.address = address
    info.manufacturer_data = {3071: bytes([3])}
    return info


def _make_cloud_entry(
    hass: HomeAssistant,
    serial: str | None = "P12345678901234",
    ble_serials: list[str] | None = None,
) -> MockConfigEntry:
    """Create a MockConfigEntry with coordinator.data.chargers populated."""
    options = {}
    if ble_serials is not None:
        options[CONF_BLE_ENABLED_SERIALS] = ble_serials

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options=options,
    )
    entry.add_to_hass(hass)

    if serial is not None:
        # Inject a fake runtime_data so the BLE step can find the charger.
        runtime_data = MagicMock()
        runtime_data.coordinator.data.chargers = {serial: MagicMock()}
        entry.runtime_data = runtime_data

    return entry


@pytest.mark.asyncio
async def test_bluetooth_step_aborts_not_supported(hass: HomeAssistant) -> None:
    """Non-Ratio advertisement should abort with not_supported."""
    service_info = _make_service_info("SOME_OTHER_DEVICE")

    with patch(
        "custom_components.ratio.config_flow.parse_advertisement",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info,
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_supported"


@pytest.mark.asyncio
async def test_bluetooth_step_aborts_cloud_account_required(
    hass: HomeAssistant,
) -> None:
    """Valid Ratio advert but no cloud entry with that serial → cloud_account_required."""
    from aioratio.ble.discovery import RatioAdvertisement

    service_info = _make_service_info("RATIO_P99999999999999", "AA:BB:CC:DD:EE:FF")

    # Entry exists but for a different serial.
    _make_cloud_entry(hass, serial="P12345678901234")

    with patch(
        "custom_components.ratio.config_flow.parse_advertisement",
        return_value=RatioAdvertisement(
            local_name="RATIO_P99999999999999", manufacturer_byte=3
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info,
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cloud_account_required"


@pytest.mark.asyncio
async def test_bluetooth_step_aborts_ble_already_configured(
    hass: HomeAssistant,
) -> None:
    """Serial already in ble_enabled_serials → ble_already_configured."""
    from aioratio.ble.discovery import RatioAdvertisement

    serial = "P12345678901234"
    service_info = _make_service_info(f"RATIO_{serial}", "AA:BB:CC:DD:EE:FF")

    _make_cloud_entry(hass, serial=serial, ble_serials=[serial])

    with patch(
        "custom_components.ratio.config_flow.parse_advertisement",
        return_value=RatioAdvertisement(
            local_name=f"RATIO_{serial}", manufacturer_byte=3
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info,
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "ble_already_configured"
    assert result["description_placeholders"] == {"serial": serial}


@pytest.mark.asyncio
async def test_bluetooth_confirm_enables_ble(hass: HomeAssistant) -> None:
    """Confirming BLE adds the serial to options and schedules a reload."""
    from aioratio.ble.discovery import RatioAdvertisement

    serial = "P12345678901234"
    address = "AA:BB:CC:DD:EE:FF"
    service_info = _make_service_info(f"RATIO_{serial}", address)

    cloud_entry = _make_cloud_entry(hass, serial=serial, ble_serials=[])

    with patch(
        "custom_components.ratio.config_flow.parse_advertisement",
        return_value=RatioAdvertisement(
            local_name=f"RATIO_{serial}", manufacturer_byte=3
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info,
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "ble_configured"
    assert serial in cloud_entry.options.get(CONF_BLE_ENABLED_SERIALS, [])
    mock_reload.assert_called_once_with(cloud_entry.entry_id)


@pytest.mark.asyncio
async def test_options_flow_removes_serial(hass: HomeAssistant) -> None:
    """Options flow: unchecking a serial removes it from ble_enabled_serials."""
    serial = "12345678901234"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={CONF_BLE_ENABLED_SERIALS: [serial]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit with the serial unchecked (False).
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={serial: False},
    )
    await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert serial not in result2["data"].get(CONF_BLE_ENABLED_SERIALS, [])
