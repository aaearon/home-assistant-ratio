"""External statistics importer for Ratio charge sessions.

Completed charge sessions are aggregated per hour on the
``ratio:energy_<serial>`` external statistic, with monotonically increasing
``sum`` (running cumulative Wh) and ``state`` set to the total energy for
that hour.

Uses ``async_add_external_statistics``; the recorder requires the source to
match the statistic_id domain prefix (``ratio``). This integration requires
HA 2025.11+: ``mean_type`` was added to the metadata in 2025.4 and
``unit_class`` in 2025.11.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

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


def floor_hour_ts(ts: int) -> int:
    """Floor a UTC epoch-seconds timestamp to the start of its hour."""
    return int(ts) - int(ts) % 3600


def _floor_hour(ts: int) -> datetime:
    """Floor a UTC epoch-seconds timestamp to the start of its hour."""
    return datetime.fromtimestamp(floor_hour_ts(ts), tz=UTC)


def build_metadata(serial: str) -> StatisticMetaData:
    """Build the StatisticMetaData dict for one charger.

    ``StatisticMeanType`` is imported lazily for the same reason the recorder
    import in ``async_import_sessions`` is: pulling the recorder package in at
    module import time is heavy and breaks tests that do not load the recorder
    integration.

    ``has_mean`` is deliberately omitted. It is ``NotRequired`` from HA
    2025.11 -- this integration's minimum -- and the column is slated for
    removal, after which ``StatisticsMeta(**meta)`` would raise TypeError on
    it.
    """
    from homeassistant.components.recorder.models.statistics import StatisticMeanType

    return {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"Ratio Charger Energy {serial}",
        "source": DOMAIN,
        "statistic_id": statistic_id_for(serial),
        # EnergyConverter.UNIT_CLASS; lets the recorder convert Wh to the
        # unit the user has configured for energy.
        "unit_class": "energy",
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


class LastStatistic(NamedTuple):
    """The last recorded row of a charger's external statistic series."""

    start_ts: int
    """Epoch seconds of the row's hour start."""

    total: float
    """The row's cumulative ``sum``."""


async def async_get_last_sum(hass: HomeAssistant, serial: str) -> float | None:
    """Return the last recorded cumulative ``sum`` for a charger's statistic.

    Returns ``None`` when the statistic has no rows at all -- distinct from a
    series whose latest ``sum`` happens to be ``0.0``. Callers must not
    collapse the two: ``None`` means "no series exists", ``0.0`` means "a
    series exists and its latest cumulative total is zero".
    """
    last = await async_get_last_statistic(hass, serial)
    return None if last is None else last.total


async def async_get_last_statistic(
    hass: HomeAssistant, serial: str
) -> LastStatistic | None:
    """Return the last recorded row of a charger's statistic, or ``None``.

    ``None`` means "no series exists" -- never confuse it with a real
    ``sum`` of ``0.0``.
    """
    # Lazy import -- pulling in the recorder package at module level is heavy
    # and breaks tests that don't load the recorder integration. Matches the
    # deferred-import convention used by ``async_import_sessions`` above.
    # ``get_instance`` is imported from ``homeassistant.helpers.recorder``
    # (its defining module) rather than ``homeassistant.components.recorder``
    # -- the latter only re-exports it without declaring it in ``__all__``,
    # which mypy's strict no-implicit-reexport check rejects.
    from homeassistant.components.recorder.statistics import get_last_statistics
    from homeassistant.helpers.recorder import get_instance

    statistic_id = statistic_id_for(serial)
    # convert_units MUST be False: the integration writes raw Wh, and True
    # would silently return a display-unit-converted value as the baseline.
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, False, {"sum"}
    )
    rows = result.get(statistic_id) if result else None
    if not rows:
        return None
    row = rows[0]
    last_sum = row.get("sum")
    start = row.get("start")
    if last_sum is None or start is None:
        return None
    # ``start`` is epoch seconds (the DB ``start_ts`` column), not a datetime.
    return LastStatistic(start_ts=int(start), total=float(last_sum))


# Re-export dt_util for tests.
__all__ = [
    "LastStatistic",
    "async_get_last_statistic",
    "async_get_last_sum",
    "async_import_sessions",
    "build_metadata",
    "build_statistics",
    "floor_hour_ts",
    "statistic_id_for",
    "dt_util",
]
