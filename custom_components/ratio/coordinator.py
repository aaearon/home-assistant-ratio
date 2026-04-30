"""DataUpdateCoordinator for the Ratio integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable

from aioratio import RatioClient
from aioratio.exceptions import (
    RatioApiError,
    RatioAuthError,
    RatioConnectionError,
)
from aioratio.models import ChargerOverview

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class RatioCoordinator(DataUpdateCoordinator[dict[str, ChargerOverview]]):
    """Coordinator that polls the Ratio cloud for charger overviews.

    The coordinator data is keyed by serial number.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: RatioClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, ChargerOverview]:
        """Fetch the latest charger overviews."""
        try:
            overviews = await self.client.chargers_overview()
        except RatioAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RatioConnectionError, RatioApiError) as err:
            raise UpdateFailed(str(err)) from err

        return {ov.serial_number: ov for ov in overviews}

    async def request_command(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a charger command and schedule an immediate refresh."""
        result = await fn(*args, **kwargs)
        await self.async_request_refresh()
        return result
