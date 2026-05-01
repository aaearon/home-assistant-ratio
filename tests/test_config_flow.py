"""Smoke tests for the Ratio config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aioratio.exceptions import RatioAuthError, RatioConnectionError, RatioError

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ratio.const import DOMAIN


@pytest.mark.asyncio
async def test_user_step_creates_entry(hass: HomeAssistant) -> None:
    """Happy path: valid credentials produce a config entry."""
    with (
        patch(
            "custom_components.ratio.config_flow._validate_credentials",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.ratio.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
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
    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioAuthError("bad creds"),
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


@pytest.mark.asyncio
async def test_user_step_connection_error(hass: HomeAssistant) -> None:
    """Connection error surfaces cannot_connect error on the user form."""
    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioConnectionError("timeout"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_user_step_unknown_ratio_error(hass: HomeAssistant) -> None:
    """Generic RatioError surfaces unknown error on the user form."""
    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioError("something"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_user_step_unexpected_exception(hass: HomeAssistant) -> None:
    """Unexpected exception surfaces unknown error on the user form."""
    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_reauth_step_success(hass: HomeAssistant) -> None:
    """Reauth flow should update password and reload on success."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ratio.config_flow._validate_credentials",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.ratio.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "new_pass"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new_pass"


@pytest.mark.asyncio
async def test_reauth_step_invalid_auth(hass: HomeAssistant) -> None:
    """Reauth flow should show error on invalid auth."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioAuthError("bad"),
    ):
        result = await entry.start_reauth_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "wrong"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_reauth_step_connection_error(hass: HomeAssistant) -> None:
    """Reauth flow should show error on connection error."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioConnectionError("timeout"),
    ):
        result = await entry.start_reauth_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_reauth_step_unknown_error(hass: HomeAssistant) -> None:
    """Reauth flow should show error on generic RatioError."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioError("hmm"),
    ):
        result = await entry.start_reauth_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_reconfigure_connection_error(hass: HomeAssistant) -> None:
    """Reconfigure should show error on connection error."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioConnectionError("timeout"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_reconfigure_unknown_error(hass: HomeAssistant) -> None:
    """Reconfigure should show error on generic RatioError."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioError("fail"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_reconfigure_unexpected_exception(hass: HomeAssistant) -> None:
    """Reconfigure should show error on unexpected exception."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_reconfigure_step_updates_credentials(hass: HomeAssistant) -> None:
    """Reconfigure step should update email and password and reload."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "old@example.com", CONF_PASSWORD: "old_pass"},
        unique_id="old@example.com",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ratio.config_flow._validate_credentials",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.ratio.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "old@example.com", CONF_PASSWORD: "new_pass"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new_pass"


@pytest.mark.asyncio
async def test_reconfigure_step_rejects_different_account(hass: HomeAssistant) -> None:
    """Reconfigure should abort if the user tries to switch accounts."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "original@example.com", CONF_PASSWORD: "pass"},
        unique_id="original@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "different@example.com", CONF_PASSWORD: "pass"},
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "account_mismatch"


@pytest.mark.asyncio
async def test_reconfigure_step_invalid_auth(hass: HomeAssistant) -> None:
    """Reconfigure should show error on invalid credentials."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ratio.config_flow._validate_credentials",
        new_callable=AsyncMock,
        side_effect=RatioAuthError("bad"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "wrong"},
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
