"""External statistics importer for Ratio charge sessions.

Completed charge sessions are aggregated per hour on the
``ratio:energy_<serial>`` external statistic, with monotonically increasing
``sum`` (running cumulative Wh) and ``state`` set to the total energy for
that hour.

Uses ``async_add_external_statistics``; the recorder requires the source to
match the statistic_id domain prefix (``ratio``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aioratio.models.history import Session
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.components.recorder.models.statistics import (  # noqa: F401
        StatisticData,
        StatisticMetaData,
    )

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _slugify_serial(serial: str) -> str:
    """Normalize a charger serial into a valid HA statistic_id slug.

    HA requires ``[a-z0-9_]+`` with no leading/trailing/double underscores.
    """
    slug = re.sub(r"[^a-z0-9]", "_", serial.lower())
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_")


def statistic_id_for(serial: str) -> str:
    """Return the external statistic_id for a charger serial."""
    return f"{DOMAIN}:energy_{_slugify_serial(serial)}"


def _floor_hour(ts: int) -> datetime:
    """Floor a UTC epoch-seconds timestamp to the start of its hour."""
    dt = datetime.fromtimestamp(int(ts), tz=UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


def build_metadata(serial: str) -> StatisticMetaData:
    """Build the StatisticMetaData dict for one charger.

    StatisticMetaData is a TypedDict in the recorder package; constructing a
    plain dict here keeps this module importable without the recorder loaded.
    """
    return {
        "has_mean": False,
        "has_sum": True,
        "name": f"Ratio Charger Energy {serial}",
        "source": DOMAIN,
        "statistic_id": statistic_id_for(serial),
        "unit_of_measurement": "Wh",
    }


def build_statistics(
    sessions: Iterable[Session],
    starting_total: float,
) -> tuple[list[StatisticData], float]:
    """Build StatisticData entries for ``sessions``.

    Sessions must already be in chronological (ascending begin) order.
    Returns ``(statistics, new_running_total)``.
    """
    # Aggregate sessions per hour — the recorder expects at most one row per
    # start interval per statistic_id.
    hourly: dict[datetime, float] = {}
    for s in sessions:
        if s.begin is None or not s.begin.time:
            _LOGGER.debug("skipping session %s without begin time", s.session_id)
            continue
        hour = _floor_hour(int(s.begin.time))
        hourly[hour] = hourly.get(hour, 0.0) + float(s.total_charging_energy or 0)

    stats: list[StatisticData] = []
    running = float(starting_total)
    for hour in sorted(hourly):
        energy = hourly[hour]
        running += energy
        stats.append({"start": hour, "state": energy, "sum": running})
    return stats, running


async def async_import_sessions(
    hass: HomeAssistant,
    serial: str,
    sessions: list[Session],
    starting_total: float,
) -> float:
    """Import ``sessions`` for ``serial`` as external statistics.

    Returns the updated running cumulative-Wh total so callers can persist it.
    """
    stats, new_total = build_statistics(sessions, starting_total)
    if not stats:
        return new_total
    metadata = build_metadata(serial)
    # Lazy import — pulling in the recorder package at module level is heavy
    # and breaks tests that don't load the recorder integration.
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    # async_add_external_statistics is a @callback that schedules a recorder job.
    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.debug(
        "queued %d external statistic(s) for %s; new total=%.2f Wh",
        len(stats),
        serial,
        new_total,
    )
    return new_total


# Re-export dt_util for tests.
__all__ = [
    "async_import_sessions",
    "build_metadata",
    "build_statistics",
    "statistic_id_for",
    "dt_util",
]
