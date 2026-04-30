"""Smoke tests for the Ratio config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.exceptions import RatioAuthError

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ratio.const import DOMAIN


def _build_client_mock(*, raises: Exception | None = None) -> MagicMock:
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    if raises is not None:
        instance.chargers_overview = AsyncMock(side_effect=raises)
    else:
        instance.chargers_overview = AsyncMock(return_value=[])
    return instance


@pytest.mark.asyncio
async def test_user_step_creates_entry(hass: HomeAssistant) -> None:
    """Happy path: valid credentials produce a config entry."""
    client = _build_client_mock()

    with patch(
        "custom_components.ratio.config_flow.RatioClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "user@example.com"
    assert result2["data"] == {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "hunter2",
    }


@pytest.mark.asyncio
async def test_user_step_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    """Auth failure surfaces invalid_auth error on the user form."""
    client = _build_client_mock(raises=RatioAuthError("bad creds"))

    with patch(
        "custom_components.ratio.config_flow.RatioClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "wrong"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
