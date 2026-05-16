"""Tests for BLE coordinator wiring in async_setup_entry."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import (
    CONF_BLE_ADDRESSES,
    CONF_BLE_ENABLED_SERIALS,
    CONF_BLE_POLL_PERIODS,
    DEFAULT_BLE_POLL_PERIOD_S,
    DOMAIN,
)

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_SERIAL = "S1"


def _make_client_mock() -> MagicMock:
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.chargers_overview = AsyncMock(return_value=[])
    return instance


def _enter_base_patches(stack: ExitStack, hass: HomeAssistant, client: MagicMock):
    """Enter all the base patches needed for async_setup_entry."""
    hist_instance = MagicMock()
    hist_instance.async_load = AsyncMock()
    hist_instance.async_config_entry_first_refresh = AsyncMock()

    coord_instance = MagicMock()
    coord_instance.async_config_entry_first_refresh = AsyncMock()
    coord_instance.async_load_preferences = AsyncMock()

    stack.enter_context(
        patch("custom_components.ratio.RatioClient", return_value=client)
    )
    stack.enter_context(
        patch("custom_components.ratio.async_setup_services", new_callable=AsyncMock)
    )
    stack.enter_context(
        patch("custom_components.ratio.RatioCoordinator", return_value=coord_instance)
    )
    stack.enter_context(
        patch(
            "custom_components.ratio.RatioHistoryCoordinator",
            return_value=hist_instance,
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ratio.async_get_clientsession", return_value=MagicMock()
        )
    )
    stack.enter_context(
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        )
    )


@pytest.mark.asyncio
async def test_ble_coordinator_started_for_enabled_serial(
    hass: HomeAssistant,
) -> None:
    """A RatioBleCoordinator is created and started for each BLE-enabled serial."""
    from custom_components.ratio import async_setup_entry

    client = _make_client_mock()

    ble_coord_instance = MagicMock()
    cancel_mock = MagicMock()
    ble_coord_instance.async_start = MagicMock(return_value=cancel_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        options={
            CONF_BLE_ENABLED_SERIALS: [_SERIAL],
            CONF_BLE_ADDRESSES: {_SERIAL: _ADDRESS},
        },
    )
    entry.add_to_hass(hass)

    with ExitStack() as stack:
        _enter_base_patches(stack, hass, client)
        mock_ble_cls = stack.enter_context(
            patch(
                "custom_components.ratio.RatioBleCoordinator",
                return_value=ble_coord_instance,
            )
        )
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_ble_cls.assert_called_once_with(
        hass=hass,
        logger=logging.getLogger("custom_components.ratio"),
        address=_ADDRESS,
        serial=_SERIAL,
        poll_period_s=DEFAULT_BLE_POLL_PERIOD_S,
    )
    ble_coord_instance.async_start.assert_called_once()
    assert entry.runtime_data.ble_coordinators == {_SERIAL: ble_coord_instance}


@pytest.mark.asyncio
async def test_no_ble_coordinators_when_not_configured(
    hass: HomeAssistant,
) -> None:
    """When no BLE options are set, ble_coordinators is empty."""
    from custom_components.ratio import async_setup_entry

    client = _make_client_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
    )
    entry.add_to_hass(hass)

    with ExitStack() as stack:
        _enter_base_patches(stack, hass, client)
        mock_ble_cls = stack.enter_context(
            patch("custom_components.ratio.RatioBleCoordinator")
        )
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_ble_cls.assert_not_called()
    assert entry.runtime_data.ble_coordinators == {}


@pytest.mark.asyncio
async def test_setup_threads_poll_period_from_options(hass: HomeAssistant) -> None:
    """A per-serial poll period in options is forwarded to RatioBleCoordinator."""
    from custom_components.ratio import async_setup_entry

    client = _make_client_mock()

    ble_coord_instance = MagicMock()
    ble_coord_instance.async_start = MagicMock(return_value=MagicMock())

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        options={
            CONF_BLE_ENABLED_SERIALS: [_SERIAL],
            CONF_BLE_ADDRESSES: {_SERIAL: _ADDRESS},
            CONF_BLE_POLL_PERIODS: {_SERIAL: 2.0},
        },
    )
    entry.add_to_hass(hass)

    with ExitStack() as stack:
        _enter_base_patches(stack, hass, client)
        mock_ble_cls = stack.enter_context(
            patch(
                "custom_components.ratio.RatioBleCoordinator",
                return_value=ble_coord_instance,
            )
        )
        result = await async_setup_entry(hass, entry)

    assert result is True
    call_kwargs = mock_ble_cls.call_args.kwargs
    assert call_kwargs["poll_period_s"] == 2.0


@pytest.mark.asyncio
async def test_options_listener_registered(
    hass: HomeAssistant,
) -> None:
    """async_setup_entry must register an options-change reload listener."""
    from custom_components.ratio import async_setup_entry

    client = _make_client_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
    )
    entry.add_to_hass(hass)

    with ExitStack() as stack:
        _enter_base_patches(stack, hass, client)
        stack.enter_context(patch("custom_components.ratio.RatioBleCoordinator"))
        mock_listener = stack.enter_context(
            patch.object(entry, "add_update_listener", wraps=entry.add_update_listener)
        )
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_listener.assert_called_once()


@pytest.mark.asyncio
async def test_ble_coordinator_skipped_when_address_unknown(
    hass: HomeAssistant,
) -> None:
    """Serial in ble_enabled_serials but no address → warning logged, no coordinator."""
    from custom_components.ratio import async_setup_entry

    client = _make_client_mock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        options={
            CONF_BLE_ENABLED_SERIALS: [_SERIAL],
            # No ble_addresses key
        },
    )
    entry.add_to_hass(hass)

    with ExitStack() as stack:
        _enter_base_patches(stack, hass, client)
        mock_ble_cls = stack.enter_context(
            patch("custom_components.ratio.RatioBleCoordinator")
        )
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_ble_cls.assert_not_called()
    assert entry.runtime_data.ble_coordinators == {}
