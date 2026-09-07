"""Recorder round-trip test for the external statistics importer.

The dict-key assertions in ``test_statistics.py`` check the shape of the
metadata payload but cannot catch a payload the recorder actually rejects --
``StatisticsMeta.from_meta`` does an unfiltered ``StatisticsMeta(**meta)``
kwargs unpack, so an unknown key raises ``TypeError`` deep inside a recorder
executor job, which the recorder swallows. The statistic is then silently
never created. This test exercises the real recorder to prove the metadata
from :func:`build_metadata` is actually accepted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.models import ChargerOverview
from aioratio.models.history import Session, SessionHistoryPage, TimeData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import (
    HISTORY_OVERLAP_SECONDS,
    RatioData,
    RatioHistoryCoordinator,
)
from custom_components.ratio.statistics import build_metadata, statistic_id_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        RecorderInstanceContextManager,
    )


def _make_entry(hass: HomeAssistant, entry_id: str = "rec1") -> MockConfigEntry:
    """Build and register a fresh (never-loaded) config entry.

    A fresh entry means an empty ``RatioHistoryCoordinator`` store -- exactly
    what a remove-and-re-add of the integration produces, which is the
    scenario issue #84 is about: the store is wiped, but the recorder
    statistic series (keyed by serial only, not by entry_id) survives.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "hunter2"},
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    return entry


def _make_main_coordinator(serials: list[str]) -> MagicMock:
    chargers = {s: ChargerOverview.from_dict({"serialNumber": s}) for s in serials}
    main = MagicMock()
    main.data = RatioData(chargers=chargers)
    return main


def _session(sid: str, serial: str, begin_ts: int, energy: int = 1000) -> Session:
    return Session(
        session_id=sid,
        charger_serial_number=serial,
        total_charging_energy=energy,
        begin=TimeData(time=begin_ts),
        end=TimeData(time=begin_ts + 600),
    )


def _assert_sum_matches_state(rows: list[dict[str, Any]]) -> None:
    """Assert the shared external-statistics invariant on a sorted-by-start
    series: for every adjacent pair, ``sum[i] - sum[i-1] == state[i]``, and
    the series never decreases. Float-tolerant.
    """
    prev_sum: float | None = None
    for row in rows:
        state = float(row["state"])
        current_sum = float(row["sum"])
        if prev_sum is not None:
            assert current_sum >= prev_sum, (
                f"sum decreased: {prev_sum} -> {current_sum}"
            )
            assert abs((current_sum - prev_sum) - state) < 1e-6, (
                f"sum delta {current_sum - prev_sum} != state {state}"
            )
        prev_sum = current_sum


@pytest.fixture
def mock_recorder_before_hass(
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Force the recorder fixtures to resolve before ``hass`` is built.

    The repo's autouse ``auto_enable_custom_integrations`` fixture (in
    ``tests/conftest.py``) pulls in ``hass`` as one of its own dependencies,
    so without this override ``hass`` would already be under construction by
    the time a test asks for the recorder, and ``recorder_db_url`` trips
    ``assert not hass_fixture_setup``. Declaring this dependency here (default
    is a no-op fixture) makes ``hass`` depend transitively on
    ``async_test_recorder`` -> ``recorder_db_url``, so those resolve first.
    """


async def test_build_metadata_is_accepted_by_the_recorder(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """``build_metadata`` output must round-trip through the real recorder.

    Regression test for the bug where an unknown key in the metadata dict
    (e.g. a deprecated or not-yet-supported field) causes
    ``StatisticsMeta(**meta)`` to raise TypeError inside the recorder, which
    is swallowed -- the statistic is never created, and callers have no way
    to notice.
    """
    async with async_test_recorder(hass):
        serial = "RECORDERTEST01"
        metadata = build_metadata(serial)
        statistic_id = metadata["statistic_id"]

        start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        stats = [{"start": start, "state": 1000.0, "sum": 1000.0}]

        async_add_external_statistics(hass, metadata, stats)
        await async_wait_recording_done(hass)

        found_metadata = await hass.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={statistic_id})
        )
        assert statistic_id in found_metadata

        period_stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass,
                start,
                None,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
        )
        assert statistic_id in period_stats
        assert period_stats[statistic_id][0]["sum"] == 1000.0


# ---------------------------------------------------------------------------
# Issue #84: non-monotonic sum after a store reset (remove-and-re-add) or a
# rejected manual backfill into an existing series.
# ---------------------------------------------------------------------------


async def test_readd_seeds_running_total_from_recorder_not_zero(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Regression for #84: a fresh (empty-store) coordinator polling a serial
    that already has a recorder statistic series must resume from the
    recorder's last cumulative sum, not restart at 0 -- which previously made
    the statistic's ``sum`` column drop by the entire prior total.
    """
    async with async_test_recorder(hass):
        serial = "REIMPORT01"
        metadata = build_metadata(serial)
        statistic_id = metadata["statistic_id"]

        seed_start = dt_util.utcnow().replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=3)
        seeded_last_sum = 5000.0
        async_add_external_statistics(
            hass,
            metadata,
            [{"start": seed_start, "state": 5000.0, "sum": seeded_last_sum}],
        )
        await async_wait_recording_done(hass)

        entry = _make_entry(hass)
        main = _make_main_coordinator([serial])
        client = MagicMock()
        new_session_energy = 1500
        new_begin = int(dt_util.utcnow().timestamp())
        s_new = _session("new-1", serial, new_begin, energy=new_session_energy)
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[s_new], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)
        assert serial not in coord._running_total

        await coord.async_config_entry_first_refresh()
        await async_wait_recording_done(hass)

        expected_total = seeded_last_sum + new_session_energy
        assert coord._running_total[serial] == expected_total

        period_stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass,
                seed_start,
                None,
                {statistic_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        rows = period_stats[statistic_id]
        assert rows[-1]["sum"] == expected_total
        _assert_sum_matches_state(rows)


async def test_manual_import_rejected_forward_of_existing_recorder_series(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """A manual backfill must be rejected whenever the serial already has any
    recorder statistics -- even for a window strictly *after* those rows,
    where the old begin_ts-vs-store-baseline guard would not have fired.
    """
    async with async_test_recorder(hass):
        serial = "FWDBF01"
        metadata = build_metadata(serial)
        statistic_id = metadata["statistic_id"]
        seed_start = dt_util.utcnow().replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(days=2)
        async_add_external_statistics(
            hass, metadata, [{"start": seed_start, "state": 1000.0, "sum": 1000.0}]
        )
        await async_wait_recording_done(hass)

        before = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass, seed_start, None, {statistic_id}, "hour", None, {"sum"}
            )
        )
        row_count_before = len(before[statistic_id])

        entry = _make_entry(hass)
        main = _make_main_coordinator([serial])
        client = MagicMock()
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)
        # Fresh coordinator -- store-based baseline is empty, so the window
        # below would have passed the *old* guard.
        assert coord._last_imported_end_time == {}

        window_begin = dt_util.utcnow()
        window_end = window_begin + timedelta(days=1)

        with pytest.raises(ServiceValidationError):
            await coord.async_import_window(
                begin_time=window_begin, end_time=window_end
            )

        client.session_history.assert_not_called()

        after = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass, seed_start, None, {statistic_id}, "hour", None, {"sum"}
            )
        )
        assert len(after[statistic_id]) == row_count_before


async def test_manual_import_rejected_when_store_baseline_missing_but_recorder_populated(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Simulates the exact issue #84 re-add scenario: after a remove-and-
    re-add, ``async_load`` hydrates an empty store (no
    ``_last_imported_end_time`` entry at all for the serial) while the
    recorder series survives. The manual-import service must still reject,
    driven by the recorder check rather than the (now-empty) store check.
    """
    async with async_test_recorder(hass):
        serial = "MISSINGBASELINE01"
        metadata = build_metadata(serial)
        async_add_external_statistics(
            hass,
            metadata,
            [
                {
                    "start": dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                    - timedelta(hours=1),
                    "state": 2000.0,
                    "sum": 2000.0,
                }
            ],
        )
        await async_wait_recording_done(hass)

        entry = _make_entry(hass)
        main = _make_main_coordinator([serial])
        client = MagicMock()
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)
        # Re-add scenario: load storage that has nothing for this serial.
        await coord.async_load()
        assert serial not in coord._last_imported_end_time

        with pytest.raises(ServiceValidationError):
            await coord.async_import_window(begin_time=dt_util.utcnow())

        client.session_history.assert_not_called()


async def test_manual_import_accepted_into_empty_series(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """No recorder rows at all for the serial -> manual import is accepted
    and writes the expected rows starting from a 0.0 baseline.
    """
    async with async_test_recorder(hass):
        serial = "EMPTYSERIES01"
        statistic_id = statistic_id_for(serial)

        entry = _make_entry(hass)
        main = _make_main_coordinator([serial])
        client = MagicMock()
        begin_ts = int(dt_util.utcnow().timestamp()) - 3600
        s1 = _session("only", serial, begin_ts, energy=750)
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[s1], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)

        result = await coord.async_import_window(begin_time=begin_ts - 60)
        await async_wait_recording_done(hass)

        assert result == {serial: 1}

        period_stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass,
                dt_util.utc_from_timestamp(begin_ts - 7200),
                None,
                {statistic_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        rows = period_stats[statistic_id]
        assert len(rows) == 1
        assert rows[0]["state"] == 750.0
        assert rows[0]["sum"] == 750.0
        _assert_sum_matches_state(rows)

        # Manual import must remain non-mutating with respect to the regular
        # polling bookkeeping.
        assert coord._running_total == {}
        assert coord._last_imported_end_time == {}


async def test_readd_does_not_rewrite_already_imported_recorder_rows(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Regression for #84: after a remove-and-re-add the store is empty, so the
    first poll fetches the full HISTORY_BACKFILL_DAYS window and the cloud
    returns sessions that were *already* imported. Those must not be rewritten
    on top of the existing recorder rows with the seeded lifetime total as
    their baseline -- that silently inflates every historical row's ``sum``.
    """
    async with async_test_recorder(hass):
        serial = "REWRITE01"
        metadata = build_metadata(serial)
        statistic_id = metadata["statistic_id"]

        top_of_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        hours = [top_of_hour - timedelta(hours=h) for h in (5, 4, 3)]
        energies = [1000.0, 2000.0, 1500.0]
        sums = [1000.0, 3000.0, 4500.0]
        async_add_external_statistics(
            hass,
            metadata,
            [
                {"start": hour, "state": energy, "sum": total}
                for hour, energy, total in zip(hours, energies, sums, strict=True)
            ],
        )
        await async_wait_recording_done(hass)

        # The cloud returns the already-imported sessions *plus* one new one.
        old_sessions = [
            _session(f"old-{i}", serial, int(hour.timestamp()) + 60, energy=int(energy))
            for i, (hour, energy) in enumerate(zip(hours, energies, strict=True))
        ]
        new_hour = top_of_hour - timedelta(hours=1)
        new_energy = 700
        new_session = _session(
            "new-1", serial, int(new_hour.timestamp()) + 60, energy=new_energy
        )

        entry = _make_entry(hass, entry_id="rewrite1")
        main = _make_main_coordinator([serial])
        client = MagicMock()
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(
                sessions=[*old_sessions, new_session], next_token=None
            )
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)
        assert serial not in coord._running_total

        await coord.async_config_entry_first_refresh()
        await async_wait_recording_done(hass)

        period_stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass,
                hours[0],
                None,
                {statistic_id},
                "hour",
                None,
                {"sum", "state"},
            )
        )
        rows = period_stats[statistic_id]
        assert len(rows) == 4
        # The three historical rows must be byte-for-byte unchanged.
        for row, energy, total in zip(rows[:3], energies, sums, strict=True):
            assert row["state"] == energy
            assert row["sum"] == total
        # The new hour continues from the seeded lifetime total.
        assert rows[3]["state"] == float(new_energy)
        assert rows[3]["sum"] == sums[-1] + new_energy
        _assert_sum_matches_state(rows)
        assert coord._running_total[serial] == sums[-1] + new_energy
        # Dropped sessions must not be reconsidered on the next poll.
        assert set(coord._seen_ids[serial]) == {
            *(s.session_id for s in old_sessions),
            "new-1",
        }


async def test_readd_seeds_cursor_past_last_recorder_hour(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """The seeded cursor must place the next fetch window at or after the end
    of the last already-written hour, so a re-add does not re-fetch (and
    re-import) 30 days of history.
    """
    async with async_test_recorder(hass):
        serial = "CURSOR01"
        metadata = build_metadata(serial)
        last_hour = dt_util.utcnow().replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=3)
        async_add_external_statistics(
            hass, metadata, [{"start": last_hour, "state": 100.0, "sum": 100.0}]
        )
        await async_wait_recording_done(hass)

        entry = _make_entry(hass, entry_id="cursor1")
        main = _make_main_coordinator([serial])
        client = MagicMock()
        boundary = int(last_hour.timestamp())
        # A session inside the already-written hour: the filter must drop it,
        # leaving nothing to import.
        stale = _session("stale-1", serial, boundary + 120, energy=999)
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[stale], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)

        await coord.async_config_entry_first_refresh()
        await async_wait_recording_done(hass)

        now_ts = int(dt_util.utcnow().timestamp())
        assert serial in coord._last_imported_end_time
        assert coord._begin_time_for(serial, now_ts) >= boundary + 3600
        # And the fetch actually used that window, not a 30-day backfill.
        assert client.session_history.await_args.kwargs["begin_time"] >= boundary + 3600
        # The dropped session was not imported, but is remembered.
        assert coord._running_total[serial] == 100.0
        assert coord._seen_ids[serial] == ["stale-1"]

        period_stats = await hass.async_add_executor_job(
            lambda: statistics_during_period(
                hass, last_hour, None, {metadata["statistic_id"]}, "hour", None, {"sum"}
            )
        )
        assert [row["sum"] for row in period_stats[metadata["statistic_id"]]] == [100.0]

        # Seeded state is persisted even though nothing was imported.
        stored = await coord._store.async_load()
        assert stored is not None
        assert stored["running_total"][serial] == 100.0
        assert stored["last_imported_end_time"][serial] == (
            boundary + 3600 + HISTORY_OVERLAP_SECONDS
        )


async def test_recorder_query_failure_propagates_not_swallowed(
    hass: HomeAssistant,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """A genuine recorder query failure (recorder loaded, but the query
    itself raises) must still propagate.

    Only the "recorder not loaded at all" case is short-circuited to a 0.0
    baseline -- swallowing a real query error here would hide a genuine
    problem behind a silently-wrong 0.0 baseline instead.
    """
    async with async_test_recorder(hass):
        serial = "QUERYFAIL01"
        entry = _make_entry(hass, entry_id="queryfail1")
        main = _make_main_coordinator([serial])
        client = MagicMock()
        client.session_history = AsyncMock(
            return_value=SessionHistoryPage(sessions=[], next_token=None)
        )
        coord = RatioHistoryCoordinator(hass, client, entry, main)

        with patch(
            "homeassistant.components.recorder.statistics.get_last_statistics",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(ConfigEntryNotReady) as excinfo:
                await coord.async_config_entry_first_refresh()
            assert isinstance(excinfo.value.__cause__, RuntimeError)
