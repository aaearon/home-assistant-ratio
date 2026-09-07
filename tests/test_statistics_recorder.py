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

from typing import TYPE_CHECKING

import pytest
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ratio.statistics import build_metadata

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        RecorderInstanceContextManager,
    )


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
