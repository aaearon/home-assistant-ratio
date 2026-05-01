"""Shared pytest fixtures for the Ratio integration tests.

Requires ``pytest-homeassistant-custom-component``. Install with:

    pip install pytest-homeassistant-custom-component
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.models.history import SessionHistoryPage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom integrations in every test."""


def _make_client_instance() -> MagicMock:
    """Build a fully-stubbed mock RatioClient instance."""
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.chargers_overview = AsyncMock(return_value=[])
    instance.start_charge = AsyncMock()
    instance.stop_charge = AsyncMock()
    instance.user_settings = AsyncMock(return_value=None)
    instance.solar_settings = AsyncMock(return_value=None)
    instance.vehicles = AsyncMock(return_value=[])
    instance.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    instance.set_user_settings = AsyncMock()
    instance.set_solar_settings = AsyncMock()
    instance.set_charge_schedule = AsyncMock()
    instance.add_vehicle = AsyncMock()
    instance.remove_vehicle = AsyncMock()
    instance.grant_upgrade_permission = AsyncMock()
    return instance


@pytest.fixture
def mock_ratio_client() -> Generator[MagicMock, None, None]:
    """Patch RatioClient where the integration imports it.

    Yields the class mock. The configured instance is accessible via
    ``mock.return_value``.
    """
    targets = (
        "custom_components.ratio.config_flow.RatioClient",
        "custom_components.ratio.RatioClient",
    )
    instance = _make_client_instance()
    with (
        patch(targets[0]) as cf_client,
        patch(targets[1]) as init_client,
        patch(
            "custom_components.ratio.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        cf_client.return_value = instance
        init_client.return_value = instance
        yield init_client


@pytest.fixture
def mock_config_entry(hass) -> MockConfigEntry:
    """Create and register a MockConfigEntry for the Ratio domain."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_integration(
    hass, mock_config_entry, mock_ratio_client
) -> MockConfigEntry:
    """Set up the Ratio integration via the real async_setup_entry path.

    After this fixture completes, ``entry.runtime_data`` contains the
    ``RatioRuntimeData`` dataclass — populated by the real setup code,
    not by manual injection.
    """
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_config_entry
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
