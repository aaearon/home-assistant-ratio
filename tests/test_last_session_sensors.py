"""Tests for last-session sensors (C1)."""

from __future__ import annotations

from unittest.mock import MagicMock

from aioratio.models import Vehicle
from aioratio.models.history import Session, TimeData

from custom_components.ratio.sensor import (
    LAST_SESSION_DESCRIPTIONS,
    RatioLastSessionSensor,
)


def _session(
    sid: str = "s1",
    serial: str = "SN001",
    energy: int = 12345,
    begin: int = 1_700_000_000,
    end: int = 1_700_003_600,
    vehicle_name: str | None = "My EV",
) -> Session:
    return Session(
        session_id=sid,
        charger_serial_number=serial,
        total_charging_energy=energy,
        begin=TimeData(time=begin) if begin else None,
        end=TimeData(time=end) if end else None,
        vehicle=Vehicle(vehicle_id="v1", vehicle_name=vehicle_name)
        if vehicle_name
        else None,
    )


def _make_history_coordinator(
    sessions_by_serial: dict[str, list[Session]],
) -> MagicMock:
    coord = MagicMock()
    coord.data = sessions_by_serial
    coord.last_update_success = True
    return coord


def _by_key(serial: str, history) -> dict[str, RatioLastSessionSensor]:
    return {
        d.key: RatioLastSessionSensor(history, serial, d)
        for d in LAST_SESSION_DESCRIPTIONS
    }


def test_last_session_values_picked_from_most_recent_session() -> None:
    older = _session(sid="old", begin=1_700_000_000, end=1_700_003_600, energy=1000)
    newer = _session(sid="new", begin=1_700_010_000, end=1_700_020_000, energy=9000)
    history = _make_history_coordinator({"SN001": [older, newer]})

    entities = _by_key("SN001", history)

    assert entities["last_session_energy"].native_value == 9000
    assert entities["last_session_duration"].native_value == 10_000
    assert (
        int(entities["last_session_started_at"].native_value.timestamp())
        == 1_700_010_000
    )
    assert (
        int(entities["last_session_ended_at"].native_value.timestamp()) == 1_700_020_000
    )
    assert entities["last_session_vehicle"].native_value == "My EV"


def test_last_session_returns_none_when_no_sessions() -> None:
    history = _make_history_coordinator({"SN001": []})
    entities = _by_key("SN001", history)
    for e in entities.values():
        assert e.native_value is None


def test_last_session_handles_missing_charger() -> None:
    history = _make_history_coordinator({})
    entities = _by_key("SN001", history)
    for e in entities.values():
        assert e.native_value is None


def test_last_session_energy_has_no_state_class() -> None:
    desc = next(d for d in LAST_SESSION_DESCRIPTIONS if d.key == "last_session_energy")
    assert desc.state_class is None
