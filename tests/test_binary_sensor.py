"""Tests for the Ratio binary sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aioratio.models import ChargerOverview
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import RatioData

SERIAL = "SN001"


def _make_overview(
    serial: str = SERIAL,
    *,
    vehicle_connected: bool = True,
    session_active: bool = False,
    charging_paused: bool = False,
    errors: list[dict] | None = None,
    charging_disabled: bool = False,
    charging_disabled_reason: str | None = None,
    charging_authorized: bool = True,
    power_reduced_by_dso: bool = False,
    charging_state: str = "idle",
    fw_update_available: bool = False,
    fw_update_allowed: bool = False,
) -> ChargerOverview:
    d: dict = {
        "serialNumber": serial,
        "chargerStatus": {
            "indicators": {
                "isVehicleConnected": vehicle_connected,
                "isChargeSessionActive": session_active,
                "isChargingPaused": charging_paused,
                "errors": errors if errors is not None else [],
                "isChargingDisabled": charging_disabled,
                "isChargingDisabledReason": charging_disabled_reason,
                "isChargingAuthorized": charging_authorized,
                "isPowerReducedByDso": power_reduced_by_dso,
                "chargingState": charging_state,
            },
            "isChargeStartAllowed": True,
            "isChargeStopAllowed": False,
        },
        "chargerFirmwareStatus": {
            "isFirmwareUpdateAvailable": fw_update_available,
            "isFirmwareUpdateAllowed": fw_update_allowed,
            "firmwareUpdateJobs": [],
        },
    }
    return ChargerOverview.from_dict(d)


# Mapping from logical key to HA entity_id suffix (derived from entity name
# for fresh installs in tests — the entity registry is empty per test).
_KEY_TO_SUFFIX = {
    "vehicle_connected": "vehicle_connected",
    "charging": "charging",
    "charge_session_active": "session_active",
    "charging_paused": "charging_paused",
    "error": "error",
    "charging_disabled": "charging_disabled",
    "charging_authorized": "charging_authorized",
    "power_reduced_by_dso": "power_reduced_by_dso",
    "firmware_update_available": "firmware_update_available",
    "firmware_update_allowed": "firmware_update_allowed",
}


def _entity_id(key: str, serial: str = SERIAL) -> str:
    suffix = _KEY_TO_SUFFIX.get(key, key)
    return f"binary_sensor.ratio_{serial.lower()}_{suffix}"


@pytest.mark.asyncio
async def test_binary_sensors_created(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """All binary sensors should be created when charger data arrives."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Entity IDs are derived from device name + entity name, not key.
    expected_suffixes = [
        "vehicle_connected",
        "charging",  # NEW: chargingState-membership truth, key="charging"
        "session_active",  # RENAMED: was "charging", now "Session active"
        "charging_paused",
        "error",
        "charging_disabled",
        "charging_authorized",
        "power_reduced_by_dso",
        "firmware_update_available",
        "firmware_update_allowed",
    ]
    for suffix in expected_suffixes:
        eid = f"binary_sensor.ratio_{SERIAL.lower()}_{suffix}"
        state = hass.states.get(eid)
        assert state is not None, f"Missing binary_sensor: {eid}"


@pytest.mark.asyncio
async def test_vehicle_connected_on(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Vehicle connected sensor should be 'on' when connected."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(vehicle_connected=True)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
async def test_vehicle_connected_off(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Vehicle connected sensor should be 'off' when disconnected."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(vehicle_connected=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_error_sensor_on_when_errors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Error sensor should be 'on' when errors list is non-empty."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(errors=[{"errorCode": 1}])
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("error"))
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
async def test_error_sensor_off_when_no_errors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Error sensor should be 'off' when errors list is empty."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(errors=[])
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("error"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_charging_disabled_extra_attributes(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Charging disabled sensor should include reason in extra attributes."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(
        charging_disabled=True,
        charging_disabled_reason="schedule",
    )
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging_disabled"))
    assert state is not None
    assert state.state == "on"
    assert state.attributes.get("reason") == "schedule"


@pytest.mark.asyncio
async def test_charging_disabled_no_reason_no_extra_attrs(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Charging disabled sensor should have no extra attrs when reason is None."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(charging_disabled=False, charging_disabled_reason=None)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging_disabled"))
    assert state is not None
    assert state.state == "off"
    assert "reason" not in state.attributes


@pytest.mark.asyncio
async def test_binary_sensor_unavailable_when_no_data(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Binary sensors should be 'unavailable' when coordinator data is None."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    # First create the entities
    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Then set data to None
    coordinator.async_set_updated_data(None)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "unavailable"


@pytest.mark.asyncio
async def test_binary_sensor_unavailable_when_charger_missing(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Binary sensors should be 'unavailable' when charger is removed from data."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview()
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    # Remove the charger
    coordinator.async_set_updated_data(RatioData(chargers={}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("vehicle_connected"))
    assert state is not None
    assert state.state == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "charging_state",
    ["Charging", "ChargingWithVentilation", "PausedByEVSE"],
)
async def test_charging_sensor_on_for_flowing_current_states(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
    charging_state: str,
) -> None:
    """The new `charging` binary sensor reports `on` iff current is (or
    could momentarily be) flowing — matches the Android app's power-display
    semantics."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(session_active=True, charging_state=charging_state)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging"))
    assert state is not None
    assert state.state == "on"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "charging_state",
    ["VehicleDetected", "Standby", "Disabled", "Offline", "NoPower", "Error"],
)
async def test_charging_sensor_off_when_session_active_but_not_charging(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
    charging_state: str,
) -> None:
    """Regression test for #40: `binary_sensor.ratio_<serial>_charging`
    must report `off` whenever current is not flowing, even if the cloud
    keeps a session record open (e.g. the VehicleDetected window after a
    user-initiated stop while the cable is still plugged in)."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(session_active=True, charging_state=charging_state)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charging"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_session_active_sensor_tracks_raw_flag(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """The renamed `session_active` binary sensor preserves the raw
    `isChargeSessionActive` semantics for debugging / advanced users; only
    its user-facing name changed (was "Charging", now "Session active")."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(session_active=True, charging_state="VehicleDetected")
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("charge_session_active"))
    assert state is not None
    assert state.state == "on"

    ov_off = _make_overview(session_active=False, charging_state="Standby")
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov_off}))
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id("charge_session_active"))
    assert state is not None
    assert state.state == "off"


@pytest.mark.asyncio
async def test_upgrade_path_preserves_legacy_entity_id(
    hass: HomeAssistant,
    mock_ratio_client: MagicMock,
) -> None:
    """Upgrade-path guarantee for #40 / #41.

    A user upgrading from <=0.11.2 already has an entity registry row
    mapping unique_id ``<serial>_charge_session_active`` to entity_id
    ``binary_sensor.ratio_<serial>_charging``. After this release:

    - The legacy entity keeps its entity_id (so automations referencing
      it by id continue to see the same value — raw
      ``isChargeSessionActive``).
    - The new ``<serial>_charging`` entity claims the next free slug,
      ``binary_sensor.ratio_<serial>_charging_2``.

    Pre-seed the registry, run setup, assert both invariants.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
    )
    config_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    legacy_entry = registry.async_get_or_create(
        domain="binary_sensor",
        platform=DOMAIN,
        unique_id=f"{SERIAL}_charge_session_active",
        suggested_object_id=f"ratio_{SERIAL.lower()}_charging",
        config_entry=config_entry,
    )
    legacy_entity_id = legacy_entry.entity_id
    assert legacy_entity_id == f"binary_sensor.ratio_{SERIAL.lower()}_charging"

    # The mock client returns one charger so both binary-sensor entities
    # are created during setup.
    client = mock_ratio_client.return_value
    from aioratio.models import Charger

    client.chargers.return_value = [Charger(serial_number=SERIAL)]
    client.chargers_overview.return_value = [
        _make_overview(charging_state="Charging", session_active=True)
    ]

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    try:
        # (a) Legacy entity is still at its pre-upgrade entity_id, still
        # pointing at the same unique_id.
        legacy_after = registry.async_get(legacy_entity_id)
        assert legacy_after is not None
        assert legacy_after.unique_id == f"{SERIAL}_charge_session_active"

        # (b) New ``charging`` entity claimed the next free slug because
        # ``_charging`` was taken.
        new_entity_id = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{SERIAL}_charging"
        )
        assert new_entity_id == f"binary_sensor.ratio_{SERIAL.lower()}_charging_2"
    finally:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_firmware_binary_sensors(
    hass: HomeAssistant,
    setup_integration,
    mock_ratio_client: MagicMock,
) -> None:
    """Firmware binary sensors should reflect firmware status."""
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    ov = _make_overview(fw_update_available=True, fw_update_allowed=False)
    coordinator.async_set_updated_data(RatioData(chargers={SERIAL: ov}))
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id("firmware_update_available"))
    assert state is not None
    assert state.state == "on"

    state = hass.states.get(_entity_id("firmware_update_allowed"))
    assert state is not None
    assert state.state == "off"
