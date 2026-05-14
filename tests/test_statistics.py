"""Tests for the external statistics importer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from aioratio.models.history import Session, TimeData
from homeassistant.components.recorder.models.statistics import StatisticData

from custom_components.ratio.statistics import (
    build_metadata,
    build_statistics,
    statistic_id_for,
)


def _sd(sd: StatisticData) -> dict[str, Any]:
    """Cast a ``StatisticData`` TypedDict to ``dict[str, Any]`` for indexing.

    Pyright marks every key in HA's ``StatisticData`` as ``NotRequired``,
    so direct indexing trips ``reportTypedDictNotRequiredAccess``. This
    helper centralizes the boundary cast.
    """
    return cast(dict[str, Any], sd)


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


# ---------------------------------------------------------------------------
# BC lock-in: ``statistic_id_for`` and the underlying ``_slugify_serial``
# regex form the durable identity of every user's external energy statistic
# series. Any change to the slugification rule will orphan years of historical
# statistics for everyone with the integration installed.
#
# These are pinned regression tests. **Do not** "clean up" or relax the
# assertions — if you genuinely need a different slug shape, you must instead
# add a forward-only migration that re-keys existing recorder rows.
# ---------------------------------------------------------------------------


def test_slug_stability_lowercase_alnum_passthrough() -> None:
    assert statistic_id_for("abc123") == "ratio:energy_abc123"
    assert statistic_id_for("zz9999") == "ratio:energy_zz9999"


def test_slug_stability_uppercase_lowercased() -> None:
    assert statistic_id_for("ABC123") == "ratio:energy_abc123"


def test_slug_stability_non_alnum_replaced_with_underscore() -> None:
    assert statistic_id_for("AB-C_123") == "ratio:energy_ab_c_123"
    assert statistic_id_for("AB.C/123") == "ratio:energy_ab_c_123"
    assert statistic_id_for("AB C 123") == "ratio:energy_ab_c_123"


def test_slug_stability_collapses_runs_of_underscores() -> None:
    assert statistic_id_for("AB---CD") == "ratio:energy_ab_cd"
    assert statistic_id_for("AB__CD") == "ratio:energy_ab_cd"
    assert statistic_id_for("AB...CD") == "ratio:energy_ab_cd"


def test_slug_stability_strips_leading_and_trailing_underscores() -> None:
    assert statistic_id_for("--AB--CD--") == "ratio:energy_ab_cd"
    assert statistic_id_for("_abc_") == "ratio:energy_abc"


def test_slug_stability_real_world_serial_formats() -> None:
    """Pinned outputs for plausible Ratio serial-number shapes."""
    # No structural insight into Ratio's serial format is publicly known, so
    # these cover the categories most likely to appear in the wild.
    assert statistic_id_for("RTO-2024-0001") == "ratio:energy_rto_2024_0001"
    assert statistic_id_for("CPC1234567890") == "ratio:energy_cpc1234567890"
    assert statistic_id_for("R-X1") == "ratio:energy_r_x1"


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
    assert _sd(stats[0])["start"] == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert _sd(stats[1])["start"] == datetime(2024, 1, 1, 13, 0, tzinfo=UTC)

    assert _sd(stats[0])["state"] == 1500.0
    assert _sd(stats[1])["state"] == 2500.0
    assert _sd(stats[0])["sum"] == 2500.0
    assert _sd(stats[1])["sum"] == 5000.0
    # Sums must be monotonic.
    assert _sd(stats[1])["sum"] >= _sd(stats[0])["sum"]


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
    assert _sd(stats[0])["state"] == 1000.0


def test_build_statistics_empty_returns_empty() -> None:
    stats, total = build_statistics([], starting_total=42.0)
    assert stats == []
    assert total == 42.0
