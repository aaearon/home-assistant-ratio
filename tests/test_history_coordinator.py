"""Tests for RatioHistoryCoordinator (pagination, dedup, backfill, restart)."""

from __future__ import annotations

from contextlib import AbstractContextManager
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


def _patch_import() -> AbstractContextManager[AsyncMock]:
    """Return a patcher context for async_import_sessions.

    The mock returns ``starting_total + sum(session.total_charging_energy)`` so
    the coordinator's running_total bookkeeping behaves correctly.
    """

    async def _fake(hass, serial, sessions, starting_total):
        return float(starting_total) + sum(s.total_charging_energy for s in sessions)

    return patch(
        "custom_components.ratio.coordinator.async_import_sessions",
        new=AsyncMock(side_effect=_fake),
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
