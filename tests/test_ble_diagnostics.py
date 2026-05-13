"""Tests for BLE section in Ratio diagnostics and bond-issue dismissal on options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import CONF_BLE_ENABLED_SERIALS, DOMAIN
from custom_components.ratio.diagnostics import async_get_config_entry_diagnostics

SERIAL = "SN001"
MAC = "AA:BB:CC:DD:EE:FF"


def _make_ble_coordinator(
    serial: str = SERIAL,
    address: str = MAC,
    last_poll_successful: bool = True,
    available: bool = True,
) -> MagicMock:
    """Build a minimal mock RatioBleCoordinator."""
    coord = MagicMock()
    coord.serial = serial
    coord.address = address
    coord.last_poll_successful = last_poll_successful
    coord.available = available
    coord.async_dismiss_bond_issue = AsyncMock()
    return coord


# ---------------------------------------------------------------------------
# Diagnostics tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_includes_ble_section(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """ble key should be present with coordinator fields when BLE coordinator exists."""
    entry = setup_integration
    ble_coord = _make_ble_coordinator(last_poll_successful=True, available=True)

    # Inject ble_coordinators onto the live runtime_data.
    entry.runtime_data.ble_coordinators = {SERIAL: ble_coord}

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert "ble" in result
    ble_section = result["ble"]
    assert SERIAL in ble_section
    charger_ble = ble_section[SERIAL]
    assert charger_ble["last_poll_successful"] is True
    assert charger_ble["available"] is True


@pytest.mark.asyncio
async def test_diagnostics_address_redacted(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """BLE coordinator address should be redacted in diagnostics output."""
    entry = setup_integration
    ble_coord = _make_ble_coordinator(address=MAC)
    entry.runtime_data.ble_coordinators = {SERIAL: ble_coord}

    result = await async_get_config_entry_diagnostics(hass, entry)

    ble_section = result["ble"]
    assert SERIAL in ble_section
    assert ble_section[SERIAL]["address"] != MAC
    assert ble_section[SERIAL]["address"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_no_ble_section_when_disabled(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """When no BLE coordinators exist, ble key should be present but empty dict."""
    entry = setup_integration
    # Do NOT inject ble_coordinators — runtime_data is a plain RatioRuntimeData dataclass
    # without that attribute, so getattr returns {}.

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert "ble" in result
    assert result["ble"] == {}


# ---------------------------------------------------------------------------
# Options flow: dismiss bond issue on disable
# ---------------------------------------------------------------------------


def _make_cloud_entry_with_ble(
    hass: HomeAssistant,
    serials: list[str],
    ble_coord: MagicMock,
) -> MockConfigEntry:
    """Build a MockConfigEntry with BLE serials in options and a fake ble_coordinator."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        unique_id="user@example.com",
        options={CONF_BLE_ENABLED_SERIALS: serials},
    )
    entry.add_to_hass(hass)

    runtime_data = MagicMock()
    runtime_data.ble_coordinators = {s: ble_coord for s in serials}
    entry.runtime_data = runtime_data

    return entry


@pytest.mark.asyncio
async def test_bond_issue_dismissed_on_disable(hass: HomeAssistant) -> None:
    """Unchecking a serial in options flow should call async_dismiss_bond_issue."""
    ble_coord = _make_ble_coordinator(serial=SERIAL)
    entry = _make_cloud_entry_with_ble(hass, [SERIAL], ble_coord)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    # Submit with serial unchecked (False).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SERIAL: False},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    ble_coord.async_dismiss_bond_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_bond_issue_not_dismissed_when_kept_enabled(hass: HomeAssistant) -> None:
    """Keeping a serial enabled should NOT call async_dismiss_bond_issue."""
    ble_coord = _make_ble_coordinator(serial=SERIAL)
    entry = _make_cloud_entry_with_ble(hass, [SERIAL], ble_coord)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    # Submit with serial kept checked (True).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={SERIAL: True},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    ble_coord.async_dismiss_bond_issue.assert_not_awaited()
