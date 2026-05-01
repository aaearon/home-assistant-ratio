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
from aioratio.models import ChargerOverview, SolarSettings, UserSettings, Vehicle
from aioratio.models.history import Session

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_KEY_PREFERENCES,
    STORAGE_VERSION,
)
from .statistics import async_import_sessions

_LOGGER = logging.getLogger(__name__)


@dataclass
class RatioData:
    """Aggregate state cached by the coordinator each cycle."""

    chargers: dict[str, ChargerOverview] = field(default_factory=dict)
    user_settings: dict[str, UserSettings] = field(default_factory=dict)
    solar_settings: dict[str, SolarSettings] = field(default_factory=dict)
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
        # Persisted via HA Store; loaded in async_load_preferences().
        self.preferred_vehicle: dict[str, str] = {}
        self._prefs_store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORAGE_KEY_PREFERENCES}",
        )

    async def async_load_preferences(self) -> None:
        """Load persisted preferred_vehicle map from disk. Empty store -> {}."""
        stored = await self._prefs_store.async_load()
        if isinstance(stored, dict):
            pv = stored.get("preferred_vehicle")
            if isinstance(pv, dict):
                # Only keep string -> string entries to be defensive.
                self.preferred_vehicle = {
                    str(k): str(v) for k, v in pv.items() if isinstance(v, str)
                }

    async def async_save_preferences(self) -> None:
        """Persist preferred_vehicle map to disk."""
        await self._prefs_store.async_save(
            {"preferred_vehicle": dict(self.preferred_vehicle)}
        )

    async def _async_update_data(self) -> RatioData:
        """Fetch chargers, then per-charger user/solar settings + vehicles in parallel."""
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

        async def _solar(serial: str) -> tuple[str, SolarSettings | None]:
            try:
                return serial, await self.client.solar_settings(serial)
            except RatioRateLimitError:
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("solar_settings(%s) failed: %s", serial, err)
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
            settings_results, solar_results, vehicles_result = await asyncio.gather(
                asyncio.gather(*(_settings(s) for s in chargers)),
                asyncio.gather(*(_solar(s) for s in chargers)),
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

        solar_settings: dict[str, SolarSettings] = {}
        for serial, settings in solar_results:
            if settings is not None:
                solar_settings[serial] = settings
            elif prev is not None and serial in prev.solar_settings:
                solar_settings[serial] = prev.solar_settings[serial]

        vehicles = (
            vehicles_result
            if vehicles_result is not None
            else (prev.vehicles if prev is not None else [])
        )

        return RatioData(
            chargers=chargers,
            user_settings=user_settings,
            solar_settings=solar_settings,
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


# ---------------------------------------------------------------------------
# History coordinator
# ---------------------------------------------------------------------------

HISTORY_SCAN_INTERVAL = 300  # 5 minutes
STORAGE_KEY_HISTORY = "history"
HISTORY_BACKFILL_DAYS = 30
HISTORY_OVERLAP_SECONDS = 3600  # 1-hour overlap on incremental polls
# Cap the dedup ID set so it can't grow unbounded across years of polling.
DEDUP_ID_LIMIT = 2000


def _session_begin(session: Session) -> int:
    """Return the begin epoch-seconds for a session, defaulting to 0."""
    if session.begin is not None and session.begin.time:
        return int(session.begin.time)
    return 0


class RatioHistoryCoordinator(DataUpdateCoordinator[dict[str, list[Session]]]):
    """Polls the Ratio cloud for completed charge sessions and feeds external statistics.

    Keyed by charger serial; values are lists of recent ``Session`` objects sorted by
    begin time. Pagination state and a small dedup set of recently-seen session IDs
    are persisted in HA storage so restarts don't re-import or skip sessions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: RatioClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}_history",
            update_interval=timedelta(seconds=HISTORY_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORAGE_KEY_HISTORY}",
        )
        # Per-serial state. Hydrated by ``async_load`` before first refresh.
        self._last_imported_end_time: dict[str, int] = {}
        self._seen_ids: dict[str, list[str]] = {}
        self._running_total: dict[str, float] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Hydrate persisted pagination + dedup + running-total state from disk."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            lie = stored.get("last_imported_end_time")
            if isinstance(lie, dict):
                self._last_imported_end_time = {
                    str(k): int(v) for k, v in lie.items() if isinstance(v, (int, float))
                }
            seen = stored.get("seen_ids")
            if isinstance(seen, dict):
                self._seen_ids = {
                    str(k): [str(x) for x in v]
                    for k, v in seen.items()
                    if isinstance(v, list)
                }
            rt = stored.get("running_total")
            if isinstance(rt, dict):
                self._running_total = {
                    str(k): float(v)
                    for k, v in rt.items()
                    if isinstance(v, (int, float))
                }
        self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "last_imported_end_time": dict(self._last_imported_end_time),
                "seen_ids": {k: list(v) for k, v in self._seen_ids.items()},
                "running_total": dict(self._running_total),
            }
        )

    def _begin_time_for(self, serial: str, now_ts: int) -> int:
        """Compute begin_time for the next session_history call for ``serial``."""
        last = self._last_imported_end_time.get(serial)
        if last is None:
            return now_ts - HISTORY_BACKFILL_DAYS * 86400
        return max(0, last - HISTORY_OVERLAP_SECONDS)

    async def _fetch_all_pages(
        self, serial: str, begin_time: int, end_time: int | None = None
    ) -> list[Session]:
        """Walk pagination tokens for one charger and return all sessions."""
        sessions: list[Session] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "begin_time": begin_time,
                "serial_number": serial,
                "next_token": next_token,
            }
            if end_time is not None:
                kwargs["end_time"] = end_time
            page = await self.client.session_history(**kwargs)
            sessions.extend(page.sessions)
            if not page.next_token:
                break
            next_token = page.next_token
        return sessions

    async def async_import_window(
        self,
        begin_time: "datetime | int",
        end_time: "datetime | int | None" = None,
    ) -> dict[str, int]:
        """Manually backfill external statistics for the given window.

        Iterates every charger known to the main coordinator, fetches sessions
        in [begin_time, end_time], and imports them as external statistics.
        Returns a per-serial count of sessions actually imported.

        Does NOT mutate the regular polling state (``_last_imported_end_time``,
        ``_seen_ids``, ``_running_total``) — manual imports are intended to fill
        gaps without disturbing the running totals used by the live poll loop.
        """
        from datetime import datetime as _dt

        def _to_epoch(v: "datetime | int") -> int:
            if isinstance(v, _dt):
                return int(v.timestamp())
            return int(v)

        begin_ts = _to_epoch(begin_time)
        end_ts = _to_epoch(end_time) if end_time is not None else None

        main = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get(
            "coordinator"
        )
        serials: list[str] = []
        if main is not None and getattr(main, "data", None) is not None:
            serials = list(main.data.chargers.keys())

        imported: dict[str, int] = {}
        for serial in serials:
            fetched = await self._fetch_all_pages(serial, begin_ts, end_ts)
            sessions = sorted(fetched, key=_session_begin)
            if not sessions:
                imported[serial] = 0
                continue
            # Use a fresh starting_total of 0 — manual imports backfill an
            # arbitrary historic window and shouldn't poison the live total.
            await async_import_sessions(self.hass, serial, sessions, 0.0)
            imported[serial] = len(sessions)
        return imported

    async def _async_update_data(self) -> dict[str, list[Session]]:
        if not self._loaded:
            await self.async_load()

        # We need the list of charger serials. Pull from the main coordinator
        # via hass.data; if it's not there yet, fall back to an empty result.
        main = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get(
            "coordinator"
        )
        serials: list[str] = []
        if main is not None and getattr(main, "data", None) is not None:
            serials = list(main.data.chargers.keys())

        now_ts = int(dt_util.utcnow().timestamp())
        result: dict[str, list[Session]] = {}

        for serial in serials:
            begin_time = self._begin_time_for(serial, now_ts)
            try:
                fetched = await self._fetch_all_pages(serial, begin_time)
            except RatioAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except RatioRateLimitError as err:
                raise UpdateFailed(f"rate limited; backing off: {err}") from err
            except (RatioConnectionError, RatioApiError) as err:
                raise UpdateFailed(str(err)) from err

            seen = set(self._seen_ids.get(serial, []))
            new_sessions: list[Session] = []
            for s in fetched:
                if s.session_id in seen:
                    continue
                seen.add(s.session_id)
                new_sessions.append(s)

            new_sessions.sort(key=_session_begin)

            if new_sessions:
                # Import statistics for the newly observed sessions in chronological order.
                self._running_total[serial] = await async_import_sessions(
                    self.hass,
                    serial,
                    new_sessions,
                    self._running_total.get(serial, 0.0),
                )
                # Update last_imported_end_time to the latest end (or begin) seen.
                latest_end = max(
                    (
                        s.end.time if s.end and s.end.time else _session_begin(s)
                        for s in new_sessions
                    ),
                    default=self._last_imported_end_time.get(serial, 0),
                )
                self._last_imported_end_time[serial] = max(
                    self._last_imported_end_time.get(serial, 0), int(latest_end)
                )
            else:
                # Keep last_imported_end_time advancing modestly so we don't keep
                # asking for 30 days every time when there genuinely is nothing.
                self._last_imported_end_time.setdefault(serial, now_ts)

            # Cap dedup IDs (FIFO).
            id_list = list(self._seen_ids.get(serial, []))
            for s in new_sessions:
                id_list.append(s.session_id)
            if len(id_list) > DEDUP_ID_LIMIT:
                id_list = id_list[-DEDUP_ID_LIMIT:]
            self._seen_ids[serial] = id_list

            # Surface the most recent N sessions to consumers (sorted asc).
            prior = (self.data or {}).get(serial, [])
            combined = prior + new_sessions
            # Deduplicate by session_id while preserving order.
            unique: dict[str, Session] = {}
            for s in combined:
                unique[s.session_id] = s
            merged = sorted(unique.values(), key=_session_begin)
            result[serial] = merged[-DEDUP_ID_LIMIT:]

        await self._async_save()
        return result
