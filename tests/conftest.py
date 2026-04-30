"""Shared pytest fixtures for the Ratio integration tests.

Requires ``pytest-homeassistant-custom-component``. Install with:

    pip install pytest-homeassistant-custom-component
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom integrations in every test."""


@pytest.fixture
def mock_ratio_client() -> Generator[MagicMock, None, None]:
    """Patch RatioClient where the integration imports it."""
    targets = (
        "custom_components.ratio.config_flow.RatioClient",
        "custom_components.ratio.RatioClient",
    )
    with patch(targets[0]) as cf_client, patch(targets[1]) as init_client:
        for mock in (cf_client, init_client):
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            instance.chargers_overview = AsyncMock(return_value=[])
            instance.start_charge = AsyncMock()
            instance.stop_charge = AsyncMock()
            mock.return_value = instance
        yield cf_client
