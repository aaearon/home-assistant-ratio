"""Tests for Ratio service helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ratio.const import ATTR_SLOTS, DOMAIN
from custom_components.ratio.services import SET_SCHEDULE_SCHEMA, _resolve_serials


def _make_device(config_entries: set[str], serial: str = "SN001") -> MagicMock:
    dev = MagicMock()
    dev.config_entries = config_entries
    dev.identifiers = {(DOMAIN, serial)}
    return dev


@pytest.mark.asyncio
async def test_resolve_serials_picks_active_entry(hass: HomeAssistant) -> None:
    """_resolve_serials should skip stale entries and pick the active one."""
    active_id = "entry_active"
    stale_id = "entry_stale"

    # Only active_id is present in hass.data[DOMAIN]
    hass.data[DOMAIN] = {active_id: {"client": MagicMock(), "coordinator": MagicMock()}}

    device = _make_device(config_entries={stale_id, active_id}, serial="SN001")

    with patch(
        "custom_components.ratio.services.dr.async_get"
    ) as mock_dr:
        mock_dr.return_value.async_get.return_value = device

        call = MagicMock()
        call.data = {"device_id": "dev_123"}

        pairs = _resolve_serials(hass, call)

    assert len(pairs) == 1
    entry_id, serial = pairs[0]
    assert entry_id == active_id
    assert serial == "SN001"


def test_schedule_schema_rejects_non_dict_slots() -> None:
    """Passing non-dict items in slots should raise vol.Invalid."""
    with pytest.raises(vol.Invalid):
        SET_SCHEDULE_SCHEMA({"device_id": "x", "slots": ["not-a-dict"]})
