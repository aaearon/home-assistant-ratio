"""Tests for the BLE Bluetooth config flow steps."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import (
    CONF_BLE_ENABLED_SERIALS,
    CONF_BLE_POLL_PERIODS,
    DOMAIN,
)
from tests.conftest import _r


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

    assert _r(result)["type"] == FlowResultType.ABORT
    assert _r(result)["reason"] == "not_supported"


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

    assert _r(result)["type"] == FlowResultType.ABORT
    assert _r(result)["reason"] == "cloud_account_required"


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

    assert _r(result)["type"] == FlowResultType.ABORT
    assert _r(result)["reason"] == "ble_already_configured"
    assert _r(result)["description_placeholders"] == {"serial": serial}


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

    assert _r(result)["type"] == FlowResultType.FORM
    assert _r(result)["step_id"] == "bluetooth_confirm"

    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={},
        )
        await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.ABORT
    assert _r(result2)["reason"] == "ble_configured"
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

    # async_step_init redirects straight into the per-charger substep.
    assert _r(result)["type"] == FlowResultType.FORM
    assert _r(result)["step_id"] == "charger"
    assert _r(result)["description_placeholders"] == {"serial": serial}
    schema_keys = {str(k) for k in _r(result)["data_schema"].schema}
    assert {"enabled", "poll_period_s"} <= schema_keys

    # Submit with the serial unchecked (False).
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": False, "poll_period_s": 3.0},
    )
    await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.CREATE_ENTRY
    assert serial not in _r(result2)["data"].get(CONF_BLE_ENABLED_SERIALS, [])


@pytest.mark.asyncio
async def test_options_flow_persists_poll_period(hass: HomeAssistant) -> None:
    """Options flow stores the per-serial poll period under CONF_BLE_POLL_PERIODS."""
    serial = "P12345678901234"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={CONF_BLE_ENABLED_SERIALS: [serial]},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": True, "poll_period_s": 1.5},
    )
    await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.CREATE_ENTRY
    assert _r(result2)["data"][CONF_BLE_POLL_PERIODS] == {serial: 1.5}


@pytest.mark.asyncio
async def test_options_flow_preserves_period_on_disable(
    hass: HomeAssistant,
) -> None:
    """Disabling a charger keeps its existing poll period entry intact.

    A user who set a custom period, then toggled BLE off, must see that
    value again on re-enable instead of a silent reset to the default.
    """
    serial = "P12345678901234"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={
            CONF_BLE_ENABLED_SERIALS: [serial],
            CONF_BLE_POLL_PERIODS: {serial: 1.5},
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    # Disable the charger; the form's poll_period_s field is irrelevant here.
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": False, "poll_period_s": 3.0},
    )
    await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.CREATE_ENTRY
    assert _r(result2)["data"][CONF_BLE_ENABLED_SERIALS] == []
    assert _r(result2)["data"][CONF_BLE_POLL_PERIODS] == {serial: 1.5}


@pytest.mark.asyncio
async def test_options_flow_prunes_orphan_period_keys(
    hass: HomeAssistant,
) -> None:
    """A saved period for a serial not in the enabled list is dropped on finalize."""
    serial = "P12345678901234"
    orphan = "P00000000000000"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={
            CONF_BLE_ENABLED_SERIALS: [serial],
            CONF_BLE_POLL_PERIODS: {serial: 2.0, orphan: 5.0},
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": True, "poll_period_s": 2.0},
    )
    await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.CREATE_ENTRY
    assert _r(result2)["data"][CONF_BLE_POLL_PERIODS] == {serial: 2.0}
    assert orphan not in _r(result2)["data"][CONF_BLE_POLL_PERIODS]


@pytest.mark.asyncio
async def test_options_flow_disable_with_blank_period(
    hass: HomeAssistant,
) -> None:
    """Disabling a charger without supplying a period must not raise InvalidData."""
    serial = "P12345678901234"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={CONF_BLE_ENABLED_SERIALS: [serial]},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    # Only toggle off; no poll_period_s provided.
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": False},
    )
    await hass.async_block_till_done()

    assert _r(result2)["type"] == FlowResultType.CREATE_ENTRY
    assert _r(result2)["data"][CONF_BLE_ENABLED_SERIALS] == []


@pytest.mark.asyncio
async def test_options_flow_multi_charger_walkthrough(
    hass: HomeAssistant,
) -> None:
    """Two serials are walked one form each; accumulation across chargers works."""
    serial_a = "P11111111111111"
    serial_b = "P22222222222222"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={CONF_BLE_ENABLED_SERIALS: [serial_a, serial_b]},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    # First charger form.
    assert _r(result)["type"] == FlowResultType.FORM
    assert _r(result)["step_id"] == "charger"
    assert _r(result)["description_placeholders"] == {"serial": serial_a}

    # Disable the first charger.
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"enabled": False},
    )

    # Second charger form.
    assert _r(result2)["type"] == FlowResultType.FORM
    assert _r(result2)["step_id"] == "charger"
    assert _r(result2)["description_placeholders"] == {"serial": serial_b}

    # Keep the second charger enabled with a custom period.
    result3 = await hass.config_entries.options.async_configure(
        result2["flow_id"],
        user_input={"enabled": True, "poll_period_s": 2.0},
    )
    await hass.async_block_till_done()

    assert _r(result3)["type"] == FlowResultType.CREATE_ENTRY
    assert _r(result3)["data"][CONF_BLE_ENABLED_SERIALS] == [serial_b]
    assert _r(result3)["data"][CONF_BLE_POLL_PERIODS][serial_b] == 2.0


@pytest.mark.parametrize("bad_period", [0.5, 60.5])
@pytest.mark.asyncio
async def test_options_flow_rejects_out_of_range_period(
    hass: HomeAssistant, bad_period: float
) -> None:
    """Out-of-range poll periods are rejected; options remain unchanged."""
    from homeassistant.data_entry_flow import InvalidData

    serial = "P12345678901234"
    original_options = {CONF_BLE_ENABLED_SERIALS: [serial]}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options=dict(original_options),
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ratio.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"enabled": True, "poll_period_s": bad_period},
        )

    # Options were not mutated: no CONF_BLE_POLL_PERIODS key persisted.
    assert CONF_BLE_POLL_PERIODS not in entry.options
    assert entry.options == original_options
