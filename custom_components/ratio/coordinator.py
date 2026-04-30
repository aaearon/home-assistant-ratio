"""DataUpdateCoordinator for the Ratio integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from aioratio import RatioClient
from aioratio.exceptions import (
    RatioApiError,
    RatioAuthError,
    RatioConnectionError,
    RatioRateLimitError,
)
from aioratio.models import ChargerOverview, UserSettings, Vehicle

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class RatioData:
    """Aggregate state cached by the coordinator each cycle."""

    chargers: dict[str, ChargerOverview] = field(default_factory=dict)
    user_settings: dict[str, UserSettings] = field(default_factory=dict)
    vehicles: list[Vehicle] = field(default_factory=list)


class RatioCoordinator(DataUpdateCoordinator[RatioData]):
    """Coordinator that polls the Ratio cloud for charger state, settings, and vehicles."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: RatioClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        # Per-charger HA-side preferred vehicle for the next start_charge call.
        # In-memory only; not persisted across HA restarts.
        self.preferred_vehicle: dict[str, str] = {}

    async def _async_update_data(self) -> RatioData:
        """Fetch chargers, then per-charger user settings + account vehicles in parallel."""
        try:
            overviews = await self.client.chargers_overview()
        except RatioAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RatioRateLimitError as err:
            # Subclass of RatioApiError — must be caught first.
            raise UpdateFailed(f"rate limited; backing off: {err}") from err
        except (RatioConnectionError, RatioApiError) as err:
            raise UpdateFailed(str(err)) from err

        chargers = {ov.serial_number: ov for ov in overviews}
        prev: RatioData | None = self.data

        async def _settings(serial: str) -> tuple[str, UserSettings | None]:
            try:
                return serial, await self.client.user_settings(serial)
            except RatioRateLimitError:
                # Re-raise so the whole cycle fails with UpdateFailed and HA
                # applies its built-in backoff — better than silently dropping
                # settings refreshes every poll.
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("user_settings(%s) failed: %s", serial, err)
                return serial, None

        async def _vehicles() -> list[Vehicle] | None:
            try:
                return await self.client.vehicles()
            except RatioRateLimitError:
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("vehicles() failed: %s", err)
                return None

        try:
            settings_results, vehicles_result = await asyncio.gather(
                asyncio.gather(*(_settings(s) for s in chargers)),
                _vehicles(),
            )
        except RatioRateLimitError as err:
            raise UpdateFailed(f"rate limited; backing off: {err}") from err

        user_settings: dict[str, UserSettings] = {}
        for serial, settings in settings_results:
            if settings is not None:
                user_settings[serial] = settings
            elif prev is not None and serial in prev.user_settings:
                user_settings[serial] = prev.user_settings[serial]

        vehicles = (
            vehicles_result
            if vehicles_result is not None
            else (prev.vehicles if prev is not None else [])
        )

        return RatioData(
            chargers=chargers,
            user_settings=user_settings,
            vehicles=vehicles,
        )

    async def request_command(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a charger command and schedule an immediate refresh."""
        name = getattr(fn, "__name__", "command")
        try:
            result = await fn(*args, **kwargs)
        except RatioRateLimitError as err:
            # Don't trigger an immediate refresh — it would just hit 429 again.
            raise HomeAssistantError(f"{name}: rate limited: {err}") from err
        except RatioConnectionError as err:
            raise HomeAssistantError(f"{name}: connection error: {err}") from err
        except RatioApiError as err:
            raise HomeAssistantError(f"{name} failed: {err}") from err
        await self.async_request_refresh()
        return result
