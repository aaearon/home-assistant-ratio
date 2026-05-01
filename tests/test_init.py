"""Tests for Ratio integration setup lifecycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.exceptions import RatioAuthError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.ratio.const import DOMAIN


def _make_config_entry(hass: HomeAssistant) -> MagicMock:
    """Create a minimal mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = {"email": "user@example.com", "password": "hunter2"}
    entry.domain = DOMAIN
    return entry


def _make_client_mock(
    *, aenter_side_effect: Exception | None = None,
) -> MagicMock:
    """Build a mock RatioClient instance."""
    instance = MagicMock()
    if aenter_side_effect:
        instance.__aenter__ = AsyncMock(side_effect=aenter_side_effect)
    else:
        instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.chargers_overview = AsyncMock(return_value=[])
    return instance


@pytest.mark.asyncio
async def test_setup_entry_calls_async_setup_services(hass: HomeAssistant) -> None:
    """async_setup_entry must call async_setup_services so services survive reload."""
    client = _make_client_mock()

    with (
        patch("custom_components.ratio.RatioClient", return_value=client),
        patch("custom_components.ratio.async_setup_services") as mock_setup_svc,
        patch("custom_components.ratio.RatioCoordinator") as mock_coord_cls,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
    ):
        coord_instance = MagicMock()
        coord_instance.async_config_entry_first_refresh = AsyncMock()
        mock_coord_cls.return_value = coord_instance

        entry = _make_config_entry(hass)
        from custom_components.ratio import async_setup_entry

        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_setup_svc.assert_awaited_once_with(hass)


@pytest.mark.asyncio
async def test_auth_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
) -> None:
    """RatioAuthError during client login must raise ConfigEntryAuthFailed."""
    client = _make_client_mock(aenter_side_effect=RatioAuthError("bad password"))

    with patch("custom_components.ratio.RatioClient", return_value=client):
        entry = _make_config_entry(hass)
        from custom_components.ratio import async_setup_entry

        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_client_cleanup_on_coordinator_failure(
    hass: HomeAssistant,
) -> None:
    """If coordinator first refresh fails, client.__aexit__ must be called."""
    client = _make_client_mock()

    with (
        patch("custom_components.ratio.RatioClient", return_value=client),
        patch("custom_components.ratio.async_setup_services", new_callable=AsyncMock),
        patch("custom_components.ratio.RatioCoordinator") as mock_coord_cls,
    ):
        coord_instance = MagicMock()
        coord_instance.async_config_entry_first_refresh = AsyncMock(
            side_effect=RuntimeError("update failed")
        )
        mock_coord_cls.return_value = coord_instance

        entry = _make_config_entry(hass)
        from custom_components.ratio import async_setup_entry

        with pytest.raises(RuntimeError, match="update failed"):
            await async_setup_entry(hass, entry)

    # Client must have been cleaned up despite the failure.
    client.__aexit__.assert_awaited_once_with(None, None, None)
