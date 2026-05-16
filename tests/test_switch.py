"""Tests for the Ratio charging switch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aioratio.models import ChargerOverview
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ratio.coordinator import RatioData

SERIAL = "SN001"


def _make_overview(
    serial: str = SERIAL,
    *,
    session_active: bool = False,
    start_allowed: bool = True,
    stop_allowed: bool = False,
    charging_state: str = "idle",
) -> ChargerOverview:
    return ChargerOverview.from_dict(
        {
            "serialNumber": serial,
            "chargerStatus": {
                "indicators": {
                    "isChargeSessionActive": session_active,
                    "isVehicleConnected": True,
                    "isChargingPaused": False,
                    "errors": [],
                    "isChargingDisabled": False,
                    "isChargingAuthorized": True,
                    "isPowerReducedByDso": False,
                    "chargingState": charging_state,
                },
                "isChargeStartAllowed": start_allowed,
                "isChargeStopAllowed": stop_allowed,
            },
        }
    )


def _entity_id(serial: str = SERIAL) -> str:
    return f"switch.ratio_{serial.lower()}_charging"


@pytest.mark.asyncio
async def test_switch_is_on_when_charging(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Switch reports 'on' when ``chargingState`` is in the active set
    (here: ``Charging``). The session-active flag alone is not sufficient
    — see ``test_switch_is_off_when_session_active_but_not_charging`` for
    the post-stop VehicleDetected case that motivated #37/#39."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        session_active=True, stop_allowed=True, charging_state="Charging"
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "charging_state",
    ["VehicleDetected", "Standby", "Disabled", "Offline", "NoPower", "Error"],
)
async def test_switch_is_off_when_session_active_but_not_charging(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
    charging_state: str,
) -> None:
    """Switch reports 'off' whenever current is not flowing, even if a
    session record is still open on the cloud side.

    Regression test for #37: after the user stopped charging mid-session
    the charger transitioned Charging -> VehicleDetected while the cloud
    kept reporting ``isChargeSessionActive=true``. The switch used to
    bounce back to 'on' on the next poll, then refused to be turned off
    because ``isChargeStopAllowed=false`` in VehicleDetected.
    """
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        session_active=True,
        start_allowed=True,
        stop_allowed=False,
        charging_state=charging_state,
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "charging_state", ["Charging", "ChargingWithVentilation", "PausedByEVSE"]
)
async def test_switch_is_on_for_all_charging_states(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
    charging_state: str,
) -> None:
    """Switch reports 'on' for any charging-flavoured state, matching the
    Android app's power-display logic."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        session_active=True, stop_allowed=True, charging_state=charging_state
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
async def test_switch_is_off_when_not_charging(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Switch reports 'off' when no charge session is active."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(session_active=False, start_allowed=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_switch_turn_on_calls_start_charge(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turning switch on should call client.start_charge."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    client = mock_ratio_client.return_value

    ov = _make_overview(session_active=False, start_allowed=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity_id()},
        blocking=True,
    )
    client.start_charge.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_turn_on_with_preferred_vehicle(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turn on should pass vehicle_id when preferred_vehicle is set."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    client = mock_ratio_client.return_value

    coordinator.preferred_vehicle[SERIAL] = "vehicle_123"
    ov = _make_overview(session_active=False, start_allowed=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity_id()},
        blocking=True,
    )
    client.start_charge.assert_awaited_once()
    _, kwargs = client.start_charge.call_args
    assert kwargs.get("vehicle_id") == "vehicle_123"


@pytest.mark.asyncio
async def test_switch_turn_on_not_allowed_raises(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turn on should raise HomeAssistantError when start not allowed."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        session_active=False,
        start_allowed=False,
        charging_state="waiting",
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _entity_id()},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_switch_turn_on_noop_when_already_on(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turn on should be a no-op when already charging."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    client = mock_ratio_client.return_value

    ov = _make_overview(
        session_active=True, stop_allowed=True, charging_state="Charging"
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity_id()},
        blocking=True,
    )
    client.start_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_turn_off_calls_stop_charge(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turning switch off should call client.stop_charge."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    client = mock_ratio_client.return_value

    ov = _make_overview(
        session_active=True, stop_allowed=True, charging_state="Charging"
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _entity_id()},
        blocking=True,
    )
    client.stop_charge.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_turn_off_not_allowed_raises(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turn off should raise HomeAssistantError when stop not allowed."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        session_active=True,
        stop_allowed=False,
        charging_state="Charging",
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": _entity_id()},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_switch_turn_off_noop_when_already_off(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Turn off should be a no-op when not charging."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    client = mock_ratio_client.return_value

    ov = _make_overview(session_active=False, start_allowed=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _entity_id()},
        blocking=True,
    )
    client.stop_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_unavailable_when_no_data(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Switch should be unavailable when coordinator has no data."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    # Push a charger first so the entity is created
    ov = _make_overview(session_active=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Then set data to None
    coordinator.async_set_updated_data(None)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "unavailable"


@pytest.mark.asyncio
async def test_switch_unavailable_when_charger_missing(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Switch should be unavailable when charger disappears from data."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(session_active=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Remove the charger from data
    coordinator.async_set_updated_data(RatioData(chargers={}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id())
    assert state is not None
    assert state.state == "unavailable"
