"""Tests for RatioHistoryCoordinator (pagination, dedup, backfill, restart)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioratio.models import ChargerOverview
from aioratio.models.history import Session, SessionHistoryPage, TimeData

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import (
    HISTORY_BACKFILL_DAYS,
    HISTORY_OVERLAP_SECONDS,
    RatioData,
    RatioHistoryCoordinator,
)


def _session(sid: str, serial: str, begin_ts: int, energy: int = 1000) -> Session:
    return Session(
        session_id=sid,
        charger_serial_number=serial,
        total_charging_energy=energy,
        begin=TimeData(time=begin_ts),
        end=TimeData(time=begin_ts + 600),
    )


def _stub_main_coordinator(
    hass: HomeAssistant, entry_id: str, serials: list[str]
) -> None:
    chargers = {
        s: ChargerOverview.from_dict({"serialNumber": s}) for s in serials
    }
    main = MagicMock()
    main.data = RatioData(chargers=chargers)
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["coordinator"] = main


def _patch_import() -> AsyncMock:
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
async def test_first_run_backfill_uses_30_days(hass: HomeAssistant) -> None:
    serial = "ABC123"
    client = MagicMock()
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[], next_token=None)
    )
    entry = MagicMock(spec=ConfigEntry, entry_id="e1")
    coord = RatioHistoryCoordinator(hass, client, entry)
    _stub_main_coordinator(hass, "e1", [serial])

    with _patch_import():
        await coord._async_update_data()

    call = client.session_history.await_args_list[0]
    begin_time = call.kwargs["begin_time"]
    now = int(time.time())
    expected = now - HISTORY_BACKFILL_DAYS * 86400
    assert abs(begin_time - expected) < 60
    assert call.kwargs["serial_number"] == serial


@pytest.mark.asyncio
async def test_dedup_across_two_polls_with_overlap(hass: HomeAssistant) -> None:
    serial = "S1"
    client = MagicMock()
    entry = MagicMock(spec=ConfigEntry, entry_id="e2")
    _stub_main_coordinator(hass, "e2", [serial])

    s1 = _session("id-1", serial, 1_700_000_000, energy=1000)
    s2 = _session("id-2", serial, 1_700_001_000, energy=2000)

    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1, s2], next_token=None)
    )
    coord = RatioHistoryCoordinator(hass, client, entry)

    with _patch_import() as mock_import:
        await coord._async_update_data()
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
        await coord._async_update_data()
        # Only s3 should be imported (s2 deduped).
        second_args = mock_import.await_args_list[0].args
        second_sessions = second_args[2]
        assert [s.session_id for s in second_sessions] == ["id-3"]
        # Running total carries forward from 3000.
        assert second_args[3] == 3000.0

    # The second begin_time must reflect the 1-hour overlap relative to the
    # last imported end time (s2.end == 1_700_001_600).
    second_call = client.session_history.await_args_list[0]
    last_end = s2.end.time
    assert second_call.kwargs["begin_time"] == last_end - HISTORY_OVERLAP_SECONDS


@pytest.mark.asyncio
async def test_running_total_persists_across_restart(hass: HomeAssistant) -> None:
    serial = "S2"
    entry = MagicMock(spec=ConfigEntry, entry_id="e3")
    _stub_main_coordinator(hass, "e3", [serial])
    client = MagicMock()

    s1 = _session("id-1", serial, 1_700_000_000, energy=1500)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s1], next_token=None)
    )
    coord1 = RatioHistoryCoordinator(hass, client, entry)
    with _patch_import():
        await coord1._async_update_data()

    coord2 = RatioHistoryCoordinator(hass, client, entry)
    await coord2.async_load()
    assert coord2._running_total[serial] == 1500.0
    assert serial in coord2._last_imported_end_time
    assert "id-1" in coord2._seen_ids[serial]

    s2 = _session("id-2", serial, 1_700_010_000, energy=2500)
    client.session_history = AsyncMock(
        return_value=SessionHistoryPage(sessions=[s2], next_token=None)
    )
    with _patch_import() as mock_import:
        await coord2._async_update_data()
        args = mock_import.await_args_list[0].args
        # Resumes from 1500 — not from 0.
        assert args[3] == 1500.0
        assert [s.session_id for s in args[2]] == ["id-2"]
    # Updated total is persisted again.
    assert coord2._running_total[serial] == 4000.0


@pytest.mark.asyncio
async def test_pagination_walks_next_tokens(hass: HomeAssistant) -> None:
    serial = "S3"
    entry = MagicMock(spec=ConfigEntry, entry_id="e4")
    _stub_main_coordinator(hass, "e4", [serial])

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
    coord = RatioHistoryCoordinator(hass, client, entry)

    with _patch_import() as mock_import:
        await coord._async_update_data()
        sessions = mock_import.await_args_list[0].args[2]
        assert [s.session_id for s in sessions] == ["a", "b", "c"]

    tokens = [c.kwargs.get("next_token") for c in client.session_history.await_args_list]
    assert tokens == [None, "t1", "t2"]
