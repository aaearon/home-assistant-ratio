"""Tests for RatioHistoryCoordinator (pagination, dedup, backfill, restart)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.models import ChargerOverview
from aioratio.models.history import Session, SessionHistoryPage, TimeData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import (
    HISTORY_BACKFILL_DAYS,
    HISTORY_OVERLAP_SECONDS,
    RatioData,
    RatioHistoryCoordinator,
)
from custom_components.ratio.sensor import _last_session
from custom_components.ratio.statistics import LastStatistic


def _session(sid: str, serial: str, begin_ts: int, energy: int = 1000) -> Session:
    return Session(
        session_id=sid,
        charger_serial_number=serial,
        total_charging_energy=energy,
        begin=TimeData(time=begin_ts),
        end=TimeData(time=begin_ts + 600),
    )


def _make_entry(hass: HomeAssistant, entry_id: str = "e1") -> MockConfigEntry:
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


@contextmanager
def _patch_import() -> Iterator[AsyncMock]:
    """Return a patcher context for async_import_sessions.

    The mock returns ``starting_total + sum(session.total_charging_energy)`` so
    the coordinator's running_total bookkeeping behaves correctly.

    Also patches ``async_get_last_statistic`` to return ``None`` (no recorder
    series) by default, so tests that don't care about recorder-baseline
    seeding keep the pre-existing "store empty -> 0.0" behavior without
    needing a real recorder set up in ``hass``. Tests exercising the seeding
    logic itself should patch ``async_get_last_statistic`` explicitly instead
    (see ``_patch_last_statistic``) and not use this helper.
    """

    async def _fake(hass, serial, sessions, starting_total):
        return float(starting_total) + sum(s.total_charging_energy for s in sessions)

    with ExitStack() as stack:
        mock_import = stack.enter_context(
            patch(
                "custom_components.ratio.coordinator.async_import_sessions",
                new=AsyncMock(side_effect=_fake),
            )
        )
        stack.enter_context(_patch_last_statistic(None))
        yield mock_import


def _patch_last_statistic(
    total: float | None = None,
    start_ts: int = 0,
) -> AbstractContextManager[AsyncMock]:
    """Return a patcher context for ``async_get_last_statistic``.

    Defaults to ``None`` (no recorder series) so tests that don't care about
    recorder-baseline seeding keep the pre-existing "store empty -> 0.0"
    behavior without needing a real recorder set up in ``hass``. ``start_ts``
    defaults to the epoch so the already-written-hour filter drops nothing.
    """
    return patch(
        "custom_components.ratio.coordinator.async_get_last_statistic",
        new=AsyncMock(
            return_value=None
            if total is None
            else LastStatistic(start_ts=start_ts, total=total)
        ),
    )


@pytest.mark.asyncio
async def test_first_run_backfill_uses_30_days(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2024-06-15T12:00:00+00:00")
    frozen_ts = int(dt_util.utcnow().timestamp())

    serial = "ABC123"
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    entry = _make_entry(hass)
    main = _make_main_coordinator([serial])
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    call = client.session_history.await_args_list[0]
    expected_begin = frozen_ts - (HISTORY_BACKFILL_DAYS * 86400)
    assert call.kwargs["begin_time"] == expected_begin
    assert call.kwargs["serial_number"] == serial


@pytest.mark.asyncio
async def test_dedup_across_two_polls_with_overlap(hass: HomeAssistant) -> None:
    serial = "S1"
    client = MagicMock()
    entry = _make_entry(hass, entry_id="e2")
    main = _make_main_coordinator([serial])

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    s2 = _session("id-2", serial, 1_700_001_000, energy=2000)

    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1, s2], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import() as mock_import:
        await coord.async_config_entry_first_refresh()
        # First call: both sessions imported in chronological order.
        first_args = mock_import.await_args_list[0].args
        assert first_args[1] == serial
        first_sessions = first_args[2]
        assert [s.session_id for s in first_sessions] == ["id-1", "id-2"]
        assert first_args[3] == 0.0

    # Second poll: API returns s2 (already seen) + new s3.
    s3 = _session("id-3", serial, 1_700_002_000, energy=500)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s2, s3], next_token=None)
    )
    with _patch_import() as mock_import:
        await coord.async_refresh()
        # Only s3 should be imported (s2 deduped).
        second_args = mock_import.await_args_list[0].args
        second_sessions = second_args[2]
        assert [s.session_id for s in second_sessions] == ["id-3"]
        # Running total carries forward from 3000.
        assert second_args[3] == 3000.0

    # The second begin_time must reflect the 1-hour overlap relative to the
    # last imported end time (s2.end == 1_700_001_600).
    second_call = client.session_history.await_args_list[0]
    assert s2.end is not None
    last_end = s2.end.time
    assert second_call.kwargs["begin_time"] == last_end - HISTORY_OVERLAP_SECONDS


@pytest.mark.asyncio
async def test_running_total_persists_across_restart(hass: HomeAssistant) -> None:
    serial = "S2"
    entry = _make_entry(hass, entry_id="e3")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1500)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord1 = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord1.async_config_entry_first_refresh()

    coord2 = RatioHistoryCoordinator(hass, client, entry, main)
    await coord2.async_load()
    assert coord2._running_total[serial] == 1500.0
    assert serial in coord2._last_imported_end_time
    assert "id-1" in coord2._seen_ids[serial]

    s2 = _session("id-2", serial, 1_700_010_000, energy=2500)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s2], next_token=None)
    )
    with _patch_import() as mock_import:
        await coord2.async_refresh()
        args = mock_import.await_args_list[0].args
        # Resumes from 1500 — not from 0.
        assert args[3] == 1500.0
        assert [s.session_id for s in args[2]] == ["id-2"]
    # Updated total is persisted again.
    assert coord2._running_total[serial] == 4000.0


@pytest.mark.asyncio
async def test_seeds_starting_total_from_recorder_when_store_empty(
    hass: HomeAssistant,
) -> None:
    """Issue #84: a store with no entry for the serial (e.g. after a
    remove-and-re-add, which wipes the per-entry_id store but not the
    per-serial recorder series) must seed ``starting_total`` from the
    recorder's last cumulative ``sum`` instead of restarting at 0.0.
    """
    hass.config.components.add("recorder")
    serial = "S_RECORDER_SEED"
    entry = _make_entry(hass, entry_id="e_recorder_seed")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    # Store has no entry for this serial (fresh coordinator, nothing loaded).
    assert serial not in coord._running_total

    with (
        _patch_last_statistic(9000.0),
        patch(
            "custom_components.ratio.coordinator.async_import_sessions",
            new=AsyncMock(
                side_effect=lambda hass, ser, sessions, starting_total: (
                    float(starting_total)
                    + sum(s.total_charging_energy for s in sessions)
                )
            ),
        ) as mock_import,
    ):
        await coord.async_config_entry_first_refresh()
        args = mock_import.await_args_list[0].args
        assert args[3] == 9000.0

    assert coord._running_total[serial] == 10000.0


@pytest.mark.asyncio
async def test_store_running_total_used_verbatim_not_combined_with_recorder(
    hass: HomeAssistant,
) -> None:
    """When the store already has a value for the serial, it must be used
    verbatim as ``starting_total`` -- the recorder must not be consulted at
    all, and in particular the two values must never be combined with
    ``max()`` or any other arbitration.
    """
    serial = "S_STORE_WINS"
    entry = _make_entry(hass, entry_id="e_store_wins")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("seed", serial, 1_700_000_000, energy=100)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord.async_config_entry_first_refresh()
    # Store now holds a known value for this serial.
    coord._running_total[serial] = 500.0

    s2 = _session("id-2", serial, 1_700_010_000, energy=250)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s2], next_token=None)
    )
    # Recorder disagrees with the store, and is larger -- a max() composition
    # would pick 9_000_000.0. The store value (500.0) must win verbatim.
    with (
        _patch_last_statistic(9_000_000.0) as mock_get_last_statistic,
        patch(
            "custom_components.ratio.coordinator.async_import_sessions",
            new=AsyncMock(
                side_effect=lambda hass, ser, sessions, starting_total: (
                    float(starting_total)
                    + sum(s.total_charging_energy for s in sessions)
                )
            ),
        ) as mock_import,
    ):
        await coord.async_refresh()
        args = mock_import.await_args_list[0].args
        assert args[3] == 500.0
    # The recorder must not even be consulted when the store already has a
    # value -- confirms the seeding is "store empty -> recorder", not an
    # unconditional composition of the two.
    mock_get_last_statistic.assert_not_awaited()


@pytest.mark.asyncio
async def test_recorder_not_loaded_seeds_zero_baseline_without_crashing(
    hass: HomeAssistant,
) -> None:
    """When the ``recorder`` integration is not set up in ``hass`` at all,
    seeding must fall back to a 0.0 baseline instead of crashing.

    ``recorder`` ships in HA's ``default_config``, but a user who does not
    use ``default_config`` can legitimately run without it. No statistics
    series can possibly exist if the recorder never ran, so 0.0 is correct
    -- this must not raise ``ConfigEntryNotReady`` (via a bare
    ``KeyError('recorder_instance')`` from ``get_instance``).

    This intentionally exercises the real ``async_get_last_statistic`` (no
    patching of it, unlike ``_patch_import``/``_patch_last_statistic``) so
    the real ``hass.config.components`` guard is under test.
    """
    assert "recorder" not in hass.config.components

    serial = "S_NO_RECORDER"
    entry = _make_entry(hass, entry_id="e_no_recorder")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    assert serial not in coord._running_total

    with patch(
        "custom_components.ratio.coordinator.async_import_sessions",
        new=AsyncMock(
            side_effect=lambda hass, ser, sessions, starting_total: (
                float(starting_total) + sum(s.total_charging_energy for s in sessions)
            )
        ),
    ) as mock_import:
        await coord.async_config_entry_first_refresh()
        args = mock_import.await_args_list[0].args
        assert args[3] == 0.0

    assert coord._running_total[serial] == 1000.0


@pytest.mark.asyncio
async def test_sessions_survive_restart_with_no_new_sessions(
    hass: HomeAssistant,
) -> None:
    """Regression: after restart, last-session sensors must not go unknown.

    Root cause: on first refresh after restart self.data is None, and
    _seen_ids already contains all previous session IDs, so new_sessions=[].
    Without persisting sessions, prior=[] and result[serial]=[] — sensors go
    to "unknown" until a brand-new charging session occurs.
    """
    serial = "S5"
    entry = _make_entry(hass, entry_id="e5")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=5000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord1 = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord1.async_config_entry_first_refresh()

    assert coord1.data is not None
    assert coord1.data[serial][0].session_id == "id-1"

    # Simulate restart: new coordinator, no new sessions from API.
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord2 = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord2.async_config_entry_first_refresh()

    # Sessions must be re-hydrated from persisted storage.
    assert coord2.data is not None
    assert serial in coord2.data
    assert len(coord2.data[serial]) == 1
    assert coord2.data[serial][0].session_id == "id-1"
    # _last_session must return the session (not None).
    last = _last_session(coord2, serial)
    assert last is not None
    assert last.session_id == "id-1"


@pytest.mark.asyncio
async def test_pagination_walks_next_tokens(hass: HomeAssistant) -> None:
    serial = "S3"
    entry = _make_entry(hass, entry_id="e4")
    main = _make_main_coordinator([serial])

    s1 = _session("a", serial, 1_700_000_000)
    s2 = _session("b", serial, 1_700_001_000)
    s3 = _session("c", serial, 1_700_002_000)

    pages = [
        SessionHistoryPage(sessions=[s1], next_token="t1"),
        SessionHistoryPage(sessions=[s2], next_token="t2"),
        SessionHistoryPage(sessions=[s3], next_token=None),
    ]
    client = MagicMock()
    client.session_history = AsyncMock(side_effect=pages)
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import() as mock_import:
        await coord.async_config_entry_first_refresh()
        sessions = mock_import.await_args_list[0].args[2]
        assert [s.session_id for s in sessions] == ["a", "b", "c"]

    tokens = [
        c.kwargs.get("next_token") for c in client.session_history.await_args_list
    ]
    assert tokens == [None, "t1", "t2"]


@pytest.mark.asyncio
async def test_pagination_terminates_on_empty_string_next_token(
    hass: HomeAssistant,
) -> None:
    """An empty-string ``next_token`` must terminate pagination, not loop.

    Some upstream APIs surface "no more pages" as ``""`` instead of ``None``;
    the loop guard in ``_fetch_all_pages`` relies on Python's ``not ""`` being
    truthy. A regression here would hang the history coordinator forever, so
    we pin the behaviour with an explicit test.
    """
    serial = "S_EMPTY_TOK"
    entry = _make_entry(hass, entry_id="e_empty_tok")
    main = _make_main_coordinator([serial])

    s1 = _session("only", serial, 1_700_000_000)
    pages = [
        SessionHistoryPage(sessions=[s1], next_token=""),
    ]
    client = MagicMock()
    client.session_history = AsyncMock(side_effect=pages)
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    # Exactly one call — empty-string token is treated as "no more pages".
    assert client.session_history.await_count == 1


async def _seed_bad_state_storage(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    serial: str,
    last_end_ts: int,
    seen_id: str,
    running_total: float,
) -> None:
    """Persist a 'sessions empty but seen_ids non-empty' state for ``serial``.

    Mirrors the storage produced by older versions of the integration that
    advanced bookkeeping but never persisted the session list.
    """
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from custom_components.ratio.const import STORAGE_VERSION  # noqa: PLC0415
    from custom_components.ratio.coordinator import (  # noqa: PLC0415
        STORAGE_KEY_HISTORY,
    )

    store: Store = Store(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.{entry.entry_id}.{STORAGE_KEY_HISTORY}",
    )
    await store.async_save(
        {
            "last_imported_end_time": {serial: last_end_ts},
            "seen_ids": {serial: [seen_id]},
            "running_total": {serial: running_total},
            "sessions": {serial: []},
        }
    )


@pytest.mark.asyncio
async def test_recovery_on_first_refresh_when_seen_ids_but_no_sessions(
    hass: HomeAssistant, freezer
) -> None:
    """Regression: storage from before session-persistence shipped left users
    with ``seen_ids`` populated but ``sessions: []`` — every poll thereafter
    fetched only the last hour, deduped to empty, and re-persisted empty.
    ``last_session_*`` sensors were stuck on "unknown" forever.

    Recovery runs once at the start of the first poll (background task, so
    setup is not blocked): detect the bad state and do a one-shot wide fetch
    that populates ``_persisted_sessions[serial]`` directly. Statistics
    import is intentionally skipped — they already contribute to
    ``running_total``.
    """
    freezer.move_to("2026-05-13T05:00:00+00:00")
    now_ts = int(dt_util.utcnow().timestamp())

    serial = "BAD_STATE"
    entry = _make_entry(hass, entry_id="e_recovery")
    main = _make_main_coordinator([serial])

    last_end_ts = now_ts - 7200
    await _seed_bad_state_storage(hass, entry, serial, last_end_ts, "237", 12000.0)

    s_old = _session("237", serial, now_ts - 86400, energy=12000)
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s_old], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    # async_load is storage-only — no network call yet.
    await coord.async_load()
    assert client.session_history.await_count == 0

    with _patch_import() as mock_import:
        await coord.async_config_entry_first_refresh()

        # Recovery must have populated _persisted_sessions directly.
        assert serial in coord._persisted_sessions
        assert [s.session_id for s in coord._persisted_sessions[serial]] == ["237"]
        # Statistics MUST NOT be re-imported — running_total already accounts.
        assert mock_import.await_count == 0

    # First call: wide recovery fetch. Second call: the normal 1-hour-overlap
    # poll inside _async_update_data.
    assert client.session_history.await_count == 2
    recovery_call = client.session_history.await_args_list[0]
    assert (
        recovery_call.kwargs["begin_time"] <= now_ts - HISTORY_BACKFILL_DAYS * 86400 + 5
    )

    # Bookkeeping must be untouched by recovery.
    assert coord._running_total[serial] == 12000.0
    assert coord._seen_ids[serial] == ["237"]

    # Subsequent polls must not retry recovery.
    client.session_history.reset_mock()
    client.session_history.return_value = SessionHistoryPage(
        sessions=[], next_token=None
    )
    await coord.async_refresh()
    # Only the normal poll — no second recovery fetch.
    assert client.session_history.await_count == 1


@pytest.mark.asyncio
async def test_recovery_skipped_for_stale_seen_ids_serial(hass: HomeAssistant) -> None:
    """A ``seen_ids`` entry for a charger no longer in the user's account
    must not trigger a cloud fetch."""
    stale_serial = "REMOVED"
    active_serial = "ACTIVE"
    entry = _make_entry(hass, entry_id="e_recovery_stale")
    main = _make_main_coordinator([active_serial])  # stale_serial not included

    await _seed_bad_state_storage(
        hass, entry, stale_serial, 1_700_000_000, "old", 1000.0
    )

    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    await coord.async_load()
    with _patch_import():
        await coord.async_config_entry_first_refresh()

    # No history call for the removed charger — only for the active one.
    called_serials = [
        c.kwargs.get("serial_number") for c in client.session_history.await_args_list
    ]
    assert stale_serial not in called_serials
    assert active_serial in called_serials


@pytest.mark.asyncio
async def test_recovery_skipped_when_sessions_already_persisted(
    hass: HomeAssistant,
) -> None:
    """No recovery fetch when persisted sessions exist."""
    serial = "GOOD_STATE"
    entry = _make_entry(hass, entry_id="e_recovery_skip")
    main = _make_main_coordinator([serial])

    # Seed via a normal first refresh so the storage has populated sessions.
    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    seed_client = MagicMock()
    seed_client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    seed_coord = RatioHistoryCoordinator(hass, seed_client, entry, main)
    with _patch_import():
        await seed_coord.async_config_entry_first_refresh()

    # Fresh coordinator on the same entry: recovery branch must skip the
    # wide-window fetch (persisted_sessions already populated). Only the
    # normal 1-hour-overlap poll should hit the client.
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    await coord.async_load()
    assert coord._persisted_sessions[serial][0].session_id == "id-1"

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    # Exactly one call — the normal poll. No recovery wide fetch.
    assert client.session_history.await_count == 1
    call = client.session_history.await_args_list[0]
    # Begin time is the 1-hour-overlap window, not a 30-day backfill.
    assert call.kwargs["begin_time"] > 1_700_000_000 - HISTORY_BACKFILL_DAYS * 86400


@pytest.mark.asyncio
async def test_idle_polls_do_not_shrink_fetch_window_past_session_begin(
    hass: HomeAssistant, freezer
) -> None:
    """Regression for #26: an idle stretch of empty polls must not advance the
    cursor past the begin time of a session that hasn't ended yet.

    Hazard scenario (overnight charge):

    1. A prior session has been imported; cursor anchored at its end time.
    2. Car is plugged in at T+1000s; charges for ~9 hours.
    3. ``session_history`` returns no completed sessions during the charge.
    4. Car is unplugged. Cloud surfaces the now-completed session whose
       ``begin = T+1000``.

    Before the fix, ``_async_update_data`` advanced ``_last_imported_end_time``
    to ``now_ts`` on every empty poll. The next poll then asked the cloud for
    sessions starting from ``now_ts - HISTORY_OVERLAP_SECONDS`` (≈ 1 hour) —
    and the just-completed session began ~9 hours earlier, outside that
    window, so it was never fetched. Sensors stayed stuck on the previous
    session until the user removed and re-added the integration.
    """
    serial = "S_LONG_IDLE"
    entry = _make_entry(hass, entry_id="e_long_idle")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    T = 1_700_000_000
    # Step 1: import a prior session so the cursor is anchored at T+600.
    s_old = _session("old", serial, T, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s_old], next_token=None)
    )
    freezer.move_to(dt_util.utc_from_timestamp(T + 800))
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord.async_config_entry_first_refresh()
    assert coord._last_imported_end_time[serial] == T + 600

    # Step 2-3: ~9 hours of idle polls during a charging session that the
    # cloud does not yet expose (session_history returns []).
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    for offset in (3600, 7200, 10800, 14400, 18000, 21600, 25200, 28800, 32400):
        freezer.move_to(dt_util.utc_from_timestamp(T + 1000 + offset))
        with _patch_import():
            await coord.async_refresh()

    # Step 4: car unplugged, cloud surfaces the now-completed session.
    new_session = Session(
        session_id="new",
        charger_serial_number=serial,
        total_charging_energy=10000,
        begin=TimeData(time=T + 1000),
        end=TimeData(time=T + 33000),
    )
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[new_session], next_token=None)
    )
    freezer.move_to(dt_util.utc_from_timestamp(T + 34000))

    with _patch_import() as mock_import:
        await coord.async_refresh()

    # The poll's begin_time MUST be wide enough to include the new session's
    # begin (T+1000). With the old "advance to now" branch the begin_time
    # would have been ~now-1h, missing the session entirely.
    last_call = client.session_history.await_args_list[-1]
    assert last_call.kwargs["begin_time"] <= T + 1000, (
        f"begin_time {last_call.kwargs['begin_time']} excludes session.begin "
        f"{T + 1000} — bug #26 has regressed"
    )
    # And the session must actually be imported.
    assert mock_import.await_count == 1
    args = mock_import.await_args_list[0].args
    assert [s.session_id for s in args[2]] == ["new"]
    # Cursor advances to the new session's end.
    assert coord._last_imported_end_time[serial] == T + 33000


@pytest.mark.asyncio
async def test_empty_history_charger_does_not_repeat_30_day_backfill(
    hass: HomeAssistant, freezer
) -> None:
    """A charger that has not yet produced any completed sessions must not
    re-fetch the full ``HISTORY_BACKFILL_DAYS`` window every poll cycle.

    The fix for #26 stopped advancing ``_last_imported_end_time`` on empty
    polls — correct, since advancing it past in-progress session begins is
    what caused the original bug. But that left brand-new chargers with no
    cursor at all, so ``_begin_time_for`` would pick the 30-day backfill
    branch on every 5-minute refresh forever (codex review of PR #28).

    A separate ``_empty_poll_watermark`` dict tracks the empty-poll fallback
    independently of the import cursor. Once a real session is imported the
    watermark is dropped and ``_last_imported_end_time`` takes over.
    """
    freezer.move_to("2026-05-14T12:00:00+00:00")
    serial = "S_NEW"
    entry = _make_entry(hass, entry_id="e_empty_hist")
    main = _make_main_coordinator([serial])
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    # First poll uses the 30-day backfill (no prior cursor or watermark).
    first_call = client.session_history.await_args_list[0]
    first_now = int(dt_util.utcnow().timestamp())
    assert first_call.kwargs["begin_time"] == first_now - HISTORY_BACKFILL_DAYS * 86400

    # Second poll, 5 minutes later — must use the 1-hour overlap window from
    # the empty-poll watermark, not the 30-day backfill again.
    freezer.move_to("2026-05-14T12:05:00+00:00")
    client.session_history.reset_mock()
    with _patch_import():
        await coord.async_refresh()
    second_call = client.session_history.await_args_list[0]
    second_now = int(dt_util.utcnow().timestamp())
    assert second_call.kwargs["begin_time"] == first_now - HISTORY_OVERLAP_SECONDS, (
        f"begin_time {second_call.kwargs['begin_time']} should be the "
        f"watermark ({first_now}) minus overlap, not a 30-day backfill"
    )
    # Sanity: definitely not a 30-day backfill on the second poll.
    assert second_call.kwargs["begin_time"] > second_now - HISTORY_BACKFILL_DAYS * 86400

    # When a real session finally arrives, the watermark must be dropped and
    # the cursor takes over — guarantees we never reintroduce drift.
    s_first = _session("first", serial, second_now - 60, energy=1234)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s_first], next_token=None)
    )
    freezer.move_to("2026-05-14T12:10:00+00:00")
    with _patch_import():
        await coord.async_refresh()
    assert serial not in coord._empty_poll_watermark
    assert serial in coord._last_imported_end_time


@pytest.mark.asyncio
async def test_empty_poll_watermark_persists_across_restart(
    hass: HomeAssistant, freezer
) -> None:
    """The empty-poll watermark must survive a HA restart so a new charger
    doesn't restart its 30-day backfill loop on every reload."""
    freezer.move_to("2026-05-14T12:00:00+00:00")
    serial = "S_NEW_RESTART"
    entry = _make_entry(hass, entry_id="e_empty_hist_restart")
    main = _make_main_coordinator([serial])
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord1 = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord1.async_config_entry_first_refresh()
    assert serial in coord1._empty_poll_watermark
    persisted_watermark = coord1._empty_poll_watermark[serial]

    # Simulate restart: fresh coordinator on the same entry. Watermark must
    # rehydrate from storage.
    coord2 = RatioHistoryCoordinator(hass, client, entry, main)
    await coord2.async_load()
    assert coord2._empty_poll_watermark.get(serial) == persisted_watermark


@pytest.mark.asyncio
async def test_load_clamps_cursor_advanced_past_persisted_session_ends(
    hass: HomeAssistant,
) -> None:
    """Self-heal for installs already affected by #26.

    Existing affected installs have ``_last_imported_end_time`` advanced
    arbitrarily far into the future relative to any actual imported session,
    because the previous "advance to now on empty poll" branch persisted
    that drift. After upgrading to the fixed code those installs would still
    be stuck — the cursor on disk is too high and the next poll's window
    too narrow — unless we clamp it back on load.

    Clamp rule: ``_last_imported_end_time[serial]`` must not exceed the
    largest end time in ``_persisted_sessions[serial]``. If the cursor is
    higher, snap it back to that latest end time. The dedup ``_seen_ids``
    set means we won't re-import sessions, just re-fetch the same window.
    """
    serial = "S_CLAMP"
    entry = _make_entry(hass, entry_id="e_clamp")
    main = _make_main_coordinator([serial])

    # Seed a normal storage shape via a real first refresh.
    s_old = _session("old", serial, 1_700_000_000, energy=1000)
    seed_client = MagicMock()
    seed_client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s_old], next_token=None)
    )
    seed_coord = RatioHistoryCoordinator(hass, seed_client, entry, main)
    with _patch_import():
        await seed_coord.async_config_entry_first_refresh()
    assert s_old.end is not None
    actual_end = s_old.end.time
    assert actual_end is not None

    # Manually advance the persisted cursor far past the actual session end,
    # mimicking the drift the old "advance to now" branch produced.
    drifted = actual_end + 7 * 86400  # +7 days
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from custom_components.ratio.const import STORAGE_VERSION  # noqa: PLC0415
    from custom_components.ratio.coordinator import (  # noqa: PLC0415
        STORAGE_KEY_HISTORY,
    )

    store: Store = Store(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.{entry.entry_id}.{STORAGE_KEY_HISTORY}",
    )
    raw = await store.async_load()
    assert isinstance(raw, dict)
    raw["last_imported_end_time"][serial] = drifted
    await store.async_save(raw)

    # Fresh coordinator on the same entry: load must clamp the cursor back
    # to the latest persisted session end.
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)
    await coord.async_load()

    assert coord._last_imported_end_time[serial] == actual_end, (
        f"cursor not clamped: {coord._last_imported_end_time[serial]} != {actual_end}"
    )


@pytest.mark.asyncio
async def test_recovery_skipped_when_seen_ids_empty(hass: HomeAssistant) -> None:
    """No recovery wide-fetch on a brand-new entry with no prior state.

    The first refresh still does a single normal poll (the 30-day backfill
    branch in ``_begin_time_for``), but recovery does not produce a second
    fetch.
    """
    serial = "FRESH"
    entry = _make_entry(hass, entry_id="e_recovery_fresh")
    main = _make_main_coordinator([serial])
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )

    coord = RatioHistoryCoordinator(hass, client, entry, main)
    with _patch_import():
        await coord.async_config_entry_first_refresh()

    # Exactly one call — the normal first poll. Recovery skipped.
    assert client.session_history.await_count == 1
    assert coord._recovery_attempted is True


# ---------------------------------------------------------------------------
# Transient connection-error / 5xx retry on session_history (issue #88)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_pages_connection_error_retries_once_then_succeeds(
    hass: HomeAssistant,
) -> None:
    """A single connection error during the history fetch is retried once."""
    from aioratio.exceptions import RatioConnectionError

    serial = "S_RETRY"
    entry = _make_entry(hass, entry_id="e_retry")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        side_effect=[
            RatioConnectionError("timeout"),
            SessionHistoryPage(sessions=[s1], next_token=None),
        ]
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with (
        _patch_import() as mock_import,
        patch(
            "custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
    ):
        await coord.async_config_entry_first_refresh()

    assert coord.last_update_success is True
    mock_sleep.assert_awaited_once()
    delay = mock_sleep.call_args.args[0]
    assert 1 <= delay <= 3
    args = mock_import.await_args_list[0].args
    assert [s.session_id for s in args[2]] == ["id-1"]


@pytest.mark.asyncio
async def test_fetch_all_pages_4xx_raises_update_failed_no_retry(
    hass: HomeAssistant,
) -> None:
    """A 4xx status during the history fetch is never retried."""
    from aioratio.exceptions import RatioApiError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    serial = "S_4XX"
    entry = _make_entry(hass, entry_id="e_4xx")
    main = _make_main_coordinator([serial])
    client = MagicMock()
    client.session_history = AsyncMock(side_effect=RatioApiError("nope", status=404))
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with (
        patch(
            "custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update_data()

    assert client.session_history.await_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_5xx_twice_with_cached_data_grants_grace(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """5xx on both the try and the retry, with cached data, is graced once.

    The whole cached dict is returned unchanged, ``last_update_stale`` is set,
    and neither statistics import nor the persisted store is touched again.
    """
    from aioratio.exceptions import RatioApiError

    serial = "S_GRACE"
    entry = _make_entry(hass, entry_id="e_grace")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import() as mock_import:
        await coord.async_config_entry_first_refresh()
    cached = coord.data
    assert mock_import.await_count == 1

    client.session_history = AsyncMock(
        side_effect=[
            RatioApiError("boom", status=500),
            RatioApiError("boom again", status=500),
        ]
    )
    save_spy = AsyncMock(wraps=coord._async_save)

    with (
        _patch_import() as mock_import2,
        patch.object(coord, "_async_save", save_spy),
        patch(
            "custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
        caplog.at_level(logging.WARNING),
    ):
        await coord.async_refresh()

    assert coord.last_update_success is True
    assert coord.last_update_stale is True
    assert coord.data is cached
    assert coord.data[serial][0].session_id == "id-1"
    mock_sleep.assert_awaited_once()
    assert any(
        record.levelno == logging.WARNING and "cached" in record.getMessage().lower()
        for record in caplog.records
    )
    mock_import2.assert_not_awaited()
    save_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_5xx_second_consecutive_graced_cycle_fails(
    hass: HomeAssistant,
) -> None:
    """A second consecutive graced cycle raises UpdateFailed (no more grace)."""
    from aioratio.exceptions import RatioApiError

    serial = "S_GRACE2"
    entry = _make_entry(hass, entry_id="e_grace2")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    client.session_history = AsyncMock(
        side_effect=[RatioApiError("1", status=500), RatioApiError("2", status=500)]
    )
    with (
        _patch_import(),
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coord.async_refresh()  # first graced cycle
    assert coord.last_update_success is True
    assert coord.last_update_stale is True

    client.session_history = AsyncMock(
        side_effect=[RatioApiError("3", status=500), RatioApiError("4", status=500)]
    )
    with (
        _patch_import(),
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coord.async_refresh()  # second consecutive failure — no more grace

    assert coord.last_update_success is False
    assert coord.last_update_stale is True


@pytest.mark.asyncio
async def test_history_5xx_twice_with_no_data_raises_update_failed(
    hass: HomeAssistant,
) -> None:
    """No grace at startup: a fresh coordinator with no cached data fails."""
    from aioratio.exceptions import RatioApiError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    serial = "S_STARTUP"
    entry = _make_entry(hass, entry_id="e_startup")
    main = _make_main_coordinator([serial])
    client = MagicMock()
    client.session_history = AsyncMock(
        side_effect=[RatioApiError("1", status=500), RatioApiError("2", status=500)]
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with (
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update_data()

    assert coord.data is None


@pytest.mark.asyncio
async def test_history_5xx_persisted_sessions_but_coord_data_none_not_graced(
    hass: HomeAssistant,
) -> None:
    """Persisted-but-not-yet-loaded-into-``data`` sessions must not enable grace.

    Grace is gated on ``coord.data is not None`` (a completed in-memory
    update), not on the on-disk store having prior sessions. A coordinator
    that has loaded persisted state (e.g. after a restart) but has not yet
    completed a successful ``_async_update_data`` cycle must still raise
    ``UpdateFailed`` on a transient failure.
    """
    from aioratio.exceptions import RatioApiError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    serial = "S_PERSISTED"
    entry = _make_entry(hass, entry_id="e_persisted")
    main = _make_main_coordinator([serial])

    # First coordinator: a normal successful refresh, persisting one session
    # (and seen_ids) to the store backing this entry.
    client1 = MagicMock()
    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client1.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord1 = RatioHistoryCoordinator(hass, client1, entry, main)
    with _patch_import():
        await coord1.async_config_entry_first_refresh()

    # Second coordinator against the same store: loads persisted sessions
    # into ``_persisted_sessions`` but never completes an update, so
    # ``coord2.data`` is still None.
    client2 = MagicMock()
    client2.session_history = AsyncMock(
        side_effect=[RatioApiError("1", status=500), RatioApiError("2", status=500)]
    )
    coord2 = RatioHistoryCoordinator(hass, client2, entry, main)
    await coord2.async_load()
    assert coord2._persisted_sessions.get(serial)
    assert coord2.data is None

    with (
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
        pytest.raises(UpdateFailed),
    ):
        await coord2._async_update_data()

    assert coord2.data is None


@pytest.mark.asyncio
async def test_history_grace_then_success_then_grace_again(
    hass: HomeAssistant,
) -> None:
    """Grace resets on a genuine success and can be granted again next time."""
    from aioratio.exceptions import RatioApiError

    serial = "S_CYCLE"
    entry = _make_entry(hass, entry_id="e_cycle")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()
    assert coord.last_update_stale is False

    client.session_history = AsyncMock(
        side_effect=[RatioApiError("1", status=500), RatioApiError("2", status=500)]
    )
    with (
        _patch_import(),
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coord.async_refresh()
    assert coord.last_update_stale is True
    assert coord.last_update_success is True

    s2 = _session("id-2", serial, 1_700_010_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s2], next_token=None)
    )
    with _patch_import():
        await coord.async_refresh()
    assert coord.last_update_stale is False
    assert coord.last_update_success is True
    assert {s.session_id for s in coord.data[serial]} == {"id-1", "id-2"}

    client.session_history = AsyncMock(
        side_effect=[RatioApiError("3", status=500), RatioApiError("4", status=500)]
    )
    with (
        _patch_import(),
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coord.async_refresh()
    assert coord.last_update_stale is True
    assert coord.last_update_success is True


@pytest.mark.asyncio
async def test_history_grace_returns_full_cached_dict_when_second_serial_fails(
    hass: HomeAssistant,
) -> None:
    """Grace is a whole-update decision, not per-serial partial data.

    Serial A succeeds with a new session while serial B fails transiently
    (after retry); the graced result must be the *previous* full cached dict
    for *both* serials — A's new session must not leak into ``coord.data``.
    """
    from aioratio.exceptions import RatioApiError

    serial_a, serial_b = "S_A", "S_B"
    entry = _make_entry(hass, entry_id="e_multi")
    main = _make_main_coordinator([serial_a, serial_b])
    client = MagicMock()

    sa1 = _session("a-1", serial_a, 1_700_000_000, energy=1000)
    sb1 = _session("b-1", serial_b, 1_700_000_000, energy=1000)
    sa2 = _session("a-2", serial_a, 1_700_010_000, energy=1000)

    queues: dict[str, list[Any]] = {
        serial_a: [
            SessionHistoryPage(sessions=[sa1], next_token=None),
            SessionHistoryPage(sessions=[sa2], next_token=None),
        ],
        serial_b: [
            SessionHistoryPage(sessions=[sb1], next_token=None),
            RatioApiError("boom1", status=500),
            RatioApiError("boom2", status=500),
        ],
    }

    def _session_history_side_effect(**kwargs: Any) -> SessionHistoryPage:
        item = queues[kwargs["serial_number"]].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client.session_history = AsyncMock(side_effect=_session_history_side_effect)
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()
    cached = coord.data
    assert set(cached.keys()) == {serial_a, serial_b}
    assert [s.session_id for s in cached[serial_a]] == ["a-1"]
    assert [s.session_id for s in cached[serial_b]] == ["b-1"]

    with (
        _patch_import(),
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coord.async_refresh()

    assert coord.last_update_success is True
    assert coord.last_update_stale is True
    assert coord.data is cached
    assert [s.session_id for s in coord.data[serial_a]] == ["a-1"]
    assert [s.session_id for s in coord.data[serial_b]] == ["b-1"]


@pytest.mark.asyncio
async def test_history_grace_does_not_lose_earlier_serial_sessions_on_recovery(
    hass: HomeAssistant,
) -> None:
    """Issue #88 two-phase fix: a later serial's graced failure must not
    strand an earlier serial's already-fetched-but-unprocessed session.

    Serial A (earlier in iteration order) fetches a new session (a-2) in the
    same cycle serial B fails transiently twice and triggers grace. Because
    fetch and processing must be two separate phases, A's a-2 must not be
    imported, deduped, or surfaced during the graced cycle -- it must be
    fetched and processed again, exactly once, on the next successful cycle.
    """
    from aioratio.exceptions import RatioApiError

    serial_a, serial_b = "S_A", "S_B"
    entry = _make_entry(hass, entry_id="e_recovery")
    main = _make_main_coordinator([serial_a, serial_b])
    client = MagicMock()

    sa1 = _session("a-1", serial_a, 1_700_000_000, energy=1000)
    sb1 = _session("b-1", serial_b, 1_700_000_000, energy=1000)
    sa2 = _session("a-2", serial_a, 1_700_010_000, energy=1000)

    queues: dict[str, list[Any]] = {
        serial_a: [
            SessionHistoryPage(sessions=[sa1], next_token=None),  # cycle 1
            SessionHistoryPage(sessions=[sa2], next_token=None),  # cycle 2 (graced)
            SessionHistoryPage(sessions=[sa2], next_token=None),  # cycle 3 (recovery)
        ],
        serial_b: [
            SessionHistoryPage(sessions=[sb1], next_token=None),  # cycle 1
            RatioApiError("boom1", status=500),  # cycle 2, first attempt
            RatioApiError("boom2", status=500),  # cycle 2, retry
            SessionHistoryPage(
                sessions=[sb1], next_token=None
            ),  # cycle 3, known/cached
        ],
    }

    def _session_history_side_effect(**kwargs: Any) -> SessionHistoryPage:
        item = queues[kwargs["serial_number"]].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client.session_history = AsyncMock(side_effect=_session_history_side_effect)
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with (
        _patch_import() as mock_import,
        patch("custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        # Cycle 1: both serials succeed, establishing the cached baseline.
        await coord.async_config_entry_first_refresh()
        cached = coord.data
        assert [s.session_id for s in cached[serial_a]] == ["a-1"]
        assert [s.session_id for s in cached[serial_b]] == ["b-1"]

        # Cycle 2: A fetches a-2 successfully; B fails transiently twice and
        # is graced. Two-phase fix: A's fetch must not be processed either.
        await coord.async_refresh()
        assert coord.last_update_success is True
        assert coord.last_update_stale is True
        assert coord.data is cached
        assert [s.session_id for s in coord.data[serial_a]] == ["a-1"]
        assert "a-2" not in coord._seen_ids.get(serial_a, [])
        assert not any(
            call.args[2] and call.args[2][0].session_id == "a-2"
            for call in mock_import.await_args_list
        )

        # Cycle 3: recovery. A re-fetches a-2 (overlap window); B returns its
        # already-known session b-1 (no-op dedup).
        await coord.async_refresh()

    assert coord.last_update_success is True
    assert coord.last_update_stale is False
    assert [s.session_id for s in coord.data[serial_a]] == ["a-1", "a-2"]

    a2_imports = [
        call
        for call in mock_import.await_args_list
        if call.args[2] and any(s.session_id == "a-2" for s in call.args[2])
    ]
    assert len(a2_imports) == 1


@pytest.mark.asyncio
async def test_history_rate_limit_not_retried_not_graced(hass: HomeAssistant) -> None:
    """A rate-limit error is never retried and never graced, even with cached data."""
    from aioratio.exceptions import RatioRateLimitError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    serial = "S_RATE"
    entry = _make_entry(hass, entry_id="e_rate")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()
    assert coord.last_update_stale is False

    client.session_history = AsyncMock(
        side_effect=RatioRateLimitError("slow", status=429)
    )
    with (
        patch(
            "custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update_data()

    assert client.session_history.await_count == 1
    mock_sleep.assert_not_awaited()
    assert coord.last_update_stale is False


@pytest.mark.asyncio
async def test_history_auth_error_not_retried_not_graced(hass: HomeAssistant) -> None:
    """An auth error is never retried and never graced, even with cached data."""
    from aioratio.exceptions import RatioAuthError
    from homeassistant.exceptions import ConfigEntryAuthFailed

    serial = "S_AUTH"
    entry = _make_entry(hass, entry_id="e_auth")
    main = _make_main_coordinator([serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry, main)

    with _patch_import():
        await coord.async_config_entry_first_refresh()

    client.session_history = AsyncMock(side_effect=RatioAuthError("expired"))
    with (
        patch(
            "custom_components.ratio.coordinator.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coord._async_update_data()

    assert client.session_history.await_count == 1
    mock_sleep.assert_not_awaited()
