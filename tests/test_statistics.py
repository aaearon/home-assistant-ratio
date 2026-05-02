"""Tests for the external statistics importer."""

from __future__ import annotations

from datetime import UTC, datetime

from aioratio.models.history import Session, TimeData

from custom_components.ratio.statistics import (
    build_metadata,
    build_statistics,
    statistic_id_for,
)


def _session(sid: str, begin_ts: int, energy: int) -> Session:
    return Session(
        session_id=sid,
        charger_serial_number="SER",
        total_charging_energy=energy,
        begin=TimeData(time=begin_ts),
        end=TimeData(time=begin_ts + 600),
    )


def test_metadata_is_consistent() -> None:
    meta = build_metadata("ABC123")
    assert meta["statistic_id"] == "ratio:energy_abc123"
    assert meta["source"] == "ratio"
    assert meta["unit_of_measurement"] == "Wh"
    assert meta["has_sum"] is True
    assert meta["has_mean"] is False
    assert meta["name"] == "Ratio Charger Energy ABC123"
    assert statistic_id_for("ABC123") == "ratio:energy_abc123"


def test_statistic_id_slugifies_serial() -> None:
    assert statistic_id_for("ABC-123") == "ratio:energy_abc_123"
    assert statistic_id_for("abc123") == "ratio:energy_abc123"
    assert statistic_id_for("EVB--X") == "ratio:energy_evb_x"


def test_build_statistics_monotonic_sum_and_hour_floor() -> None:
    # Pick timestamps inside two different hours.
    # 2024-01-01 12:34:56 UTC = 1704112496
    t1 = 1704112496
    # 2024-01-01 13:05:00 UTC = 1704114300
    t2 = 1704114300
    sessions = [_session("a", t1, 1500), _session("b", t2, 2500)]

    stats, total = build_statistics(sessions, starting_total=1000.0)

    assert total == 1000.0 + 1500.0 + 2500.0
    assert len(stats) == 2

    # Hours should be floored.
    assert stats[0]["start"] == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert stats[1]["start"] == datetime(2024, 1, 1, 13, 0, tzinfo=UTC)

    assert stats[0]["state"] == 1500.0
    assert stats[1]["state"] == 2500.0
    assert stats[0]["sum"] == 2500.0
    assert stats[1]["sum"] == 5000.0
    # Sums must be monotonic.
    assert stats[1]["sum"] >= stats[0]["sum"]


def test_build_statistics_skips_sessions_without_begin() -> None:
    bad = Session(
        session_id="x",
        charger_serial_number="SER",
        total_charging_energy=500,
        begin=None,
        end=None,
    )
    good = _session("g", 1704112496, 1000)
    stats, total = build_statistics([bad, good], starting_total=0.0)
    assert len(stats) == 1
    assert total == 1000.0
    assert stats[0]["state"] == 1000.0


def test_build_statistics_empty_returns_empty() -> None:
    stats, total = build_statistics([], starting_total=42.0)
    assert stats == []
    assert total == 42.0
