"""DataUpdateCoordinator for the Ratio integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime as _datetime_type
from datetime import timedelta
from typing import Any

from aioratio import RatioClient
from aioratio.exceptions import (
    RatioApiError,
    RatioAuthError,
    RatioConnectionError,
    RatioRateLimitError,
)
from aioratio.models import (
    ChargerOverview,
    CpmsConfig,
    InstallerOcppSettings,
    SolarSettings,
    UserSettings,
    Vehicle,
)
from aioratio.models.diagnostics import ChargerDiagnostics
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
    diagnostics: dict[str, ChargerDiagnostics] = field(default_factory=dict)
    ocpp_settings: dict[str, InstallerOcppSettings] = field(default_factory=dict)
    cpms_options: dict[str, list[CpmsConfig]] = field(default_factory=dict)


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
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        # Per-charger HA-side preferred vehicle for the next start_charge call.
        # Persisted via HA Store; loaded in async_load_preferences().
        self.preferred_vehicle: dict[str, str] = {}
        # Track last CPMS fetch time; refresh at most every 10 minutes.
        self._cpms_last_fetch: _datetime_type | None = None
        self._prefs_store: Store[dict[str, Any]] = Store(
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

        async def _diagnostics(serial: str) -> tuple[str, ChargerDiagnostics | None]:
            try:
                return serial, await self.client.diagnostics(serial)
            except RatioRateLimitError:
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("diagnostics(%s) failed: %s", serial, err)
                return serial, None

        async def _ocpp_settings(
            serial: str,
        ) -> tuple[str, InstallerOcppSettings | None]:
            try:
                return serial, await self.client.ocpp_settings(serial)
            except RatioRateLimitError:
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("ocpp_settings(%s) failed: %s", serial, err)
                return serial, None

        async def _cpms_options(serial: str) -> tuple[str, list[CpmsConfig] | None]:
            try:
                return serial, await self.client.cpms_options(serial)
            except RatioRateLimitError:
                raise
            except (RatioConnectionError, RatioApiError) as err:
                _LOGGER.debug("cpms_options(%s) failed: %s", serial, err)
                return serial, None

        # Refresh CPMS at most every 10 minutes regardless of how many
        # coordinator updates occur (command-triggered refreshes advance
        # the old tick counter too quickly).
        now = dt_util.utcnow()
        fetch_cpms = self._cpms_last_fetch is None or (
            now - self._cpms_last_fetch
        ) >= timedelta(minutes=10)
        if fetch_cpms:
            self._cpms_last_fetch = now

        try:
            (
                settings_results,
                solar_results,
                vehicles_result,
                diagnostics_results,
                ocpp_results,
                cpms_results,
            ) = await asyncio.gather(
                asyncio.gather(*(_settings(s) for s in chargers)),
                asyncio.gather(*(_solar(s) for s in chargers)),
                _vehicles(),
                asyncio.gather(*(_diagnostics(s) for s in chargers)),
                asyncio.gather(*(_ocpp_settings(s) for s in chargers)),
                asyncio.gather(*(_cpms_options(s) for s in chargers))
                if fetch_cpms
                else asyncio.gather(),
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
        for solar_serial, solar_setting in solar_results:
            if solar_setting is not None:
                solar_settings[solar_serial] = solar_setting
            elif prev is not None and solar_serial in prev.solar_settings:
                solar_settings[solar_serial] = prev.solar_settings[solar_serial]

        vehicles = (
            vehicles_result
            if vehicles_result is not None
            else (prev.vehicles if prev is not None else [])
        )

        diagnostics: dict[str, ChargerDiagnostics] = {}
        for serial, diag in diagnostics_results:
            if diag is not None:
                diagnostics[serial] = diag
            elif prev is not None and serial in prev.diagnostics:
                diagnostics[serial] = prev.diagnostics[serial]

        ocpp_settings_map: dict[str, InstallerOcppSettings] = {}
        for serial, ocpp in ocpp_results:
            if ocpp is not None:
                ocpp_settings_map[serial] = ocpp
            elif prev is not None and serial in prev.ocpp_settings:
                ocpp_settings_map[serial] = prev.ocpp_settings[serial]

        cpms_options_map: dict[str, list[CpmsConfig]] = {}
        if fetch_cpms:
            for serial, opts in cpms_results:
                if opts is not None:
                    cpms_options_map[serial] = opts
                elif prev is not None and serial in prev.cpms_options:
                    cpms_options_map[serial] = prev.cpms_options[serial]
        elif prev is not None:
            cpms_options_map = dict(prev.cpms_options)

        return RatioData(
            chargers=chargers,
            user_settings=user_settings,
            solar_settings=solar_settings,
            vehicles=vehicles,
            diagnostics=diagnostics,
            ocpp_settings=ocpp_settings_map,
            cpms_options=cpms_options_map,
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
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"command": name, "error": str(err)},
            ) from err
        except RatioConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="connection_error",
                translation_placeholders={"command": name, "error": str(err)},
            ) from err
        except RatioApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"command": name, "error": str(err)},
            ) from err
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
# How many recent sessions to persist so they survive a HA restart.
HISTORY_PERSIST_SESSIONS = 50


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
        main_coordinator: RatioCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}_history",
            update_interval=timedelta(seconds=HISTORY_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        self._main_coordinator = main_coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORAGE_KEY_HISTORY}",
        )
        # Per-serial state. Hydrated by ``async_load`` before first refresh.
        self._last_imported_end_time: dict[str, int] = {}
        self._seen_ids: dict[str, list[str]] = {}
        self._running_total: dict[str, float] = {}
        # Recent sessions persisted to survive HA restarts (self.data is None
        # on first refresh after restart; this seeds the surface list).
        self._persisted_sessions: dict[str, list[Session]] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Hydrate persisted pagination + dedup + running-total state from disk."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            lie = stored.get("last_imported_end_time")
            if isinstance(lie, dict):
                self._last_imported_end_time = {
                    str(k): int(v)
                    for k, v in lie.items()
                    if isinstance(v, (int, float))
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
            raw_sessions = stored.get("sessions")
            if isinstance(raw_sessions, dict):
                for serial_key, raw_list in raw_sessions.items():
                    if not isinstance(raw_list, list):
                        continue
                    parsed: list[Session] = []
                    for raw_s in raw_list:
                        if isinstance(raw_s, dict):
                            try:
                                parsed.append(Session.from_dict(raw_s))
                            except Exception:  # noqa: BLE001
                                pass
                    self._persisted_sessions[str(serial_key)] = parsed
        self._loaded = True

    async def _async_save(self, latest_result: dict[str, list[Session]]) -> None:
        sessions_to_persist: dict[str, list[dict[str, Any]]] = {
            serial: [s.to_dict() for s in session_list[-HISTORY_PERSIST_SESSIONS:]]
            for serial, session_list in latest_result.items()
        }
        await self._store.async_save(
            {
                "last_imported_end_time": dict(self._last_imported_end_time),
                "seen_ids": {k: list(v) for k, v in self._seen_ids.items()},
                "running_total": dict(self._running_total),
                "sessions": sessions_to_persist,
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
        begin_time: _datetime_type | int,
        end_time: _datetime_type | int | None = None,
    ) -> dict[str, int]:
        """Manually backfill external statistics for the given window.

        Iterates every charger known to the main coordinator, fetches sessions
        in [begin_time, end_time], and imports them as external statistics.
        Returns a per-serial count of sessions actually imported.

        Does NOT mutate the regular polling state (``_last_imported_end_time``,
        ``_seen_ids``, ``_running_total``) — manual imports are intended to fill
        gaps without disturbing the running totals used by the live poll loop.

        Note: if begin_time predates existing live-imported sessions, the
        backfilled statistics will have sum=0 as their baseline and will not
        be monotonically consistent with live-imported points. Use this service
        for initial setup or gap-filling only; re-adding the integration resets
        the live baseline cleanly.
        """

        def _to_epoch(v: _datetime_type | int) -> int:
            if isinstance(v, _datetime_type):
                return int(v.timestamp())
            return int(v)

        begin_ts = _to_epoch(begin_time)
        end_ts = _to_epoch(end_time) if end_time is not None else None

        serials: list[str] = []
        if self._main_coordinator.data is not None:
            serials = list(self._main_coordinator.data.chargers.keys())

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
            imported[serial] = sum(
                1
                for s in sessions
                if s.begin is not None and getattr(s.begin, "time", None)
            )
        return imported

    async def _async_update_data(self) -> dict[str, list[Session]]:
        if not self._loaded:
            await self.async_load()

        # We need the list of charger serials from the main coordinator.
        serials: list[str] = []
        if self._main_coordinator.data is not None:
            serials = list(self._main_coordinator.data.chargers.keys())

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
                # Advance to now even when no new sessions so the next poll
                # uses a fresh window rather than re-fetching the same range.
                self._last_imported_end_time[serial] = max(
                    self._last_imported_end_time.get(serial, 0), now_ts
                )

            # Cap dedup IDs (FIFO).
            id_list = list(self._seen_ids.get(serial, []))
            for s in new_sessions:
                id_list.append(s.session_id)
            if len(id_list) > DEDUP_ID_LIMIT:
                id_list = id_list[-DEDUP_ID_LIMIT:]
            self._seen_ids[serial] = id_list

            # Surface the most recent N sessions to consumers (sorted asc).
            # Fall back to persisted sessions on first refresh after restart
            # (self.data is None until the coordinator has completed at least
            # one successful update).
            prior = (self.data or self._persisted_sessions).get(serial, [])
            combined = prior + new_sessions
            # Deduplicate by session_id while preserving order.
            unique: dict[str, Session] = {}
            for s in combined:
                unique[s.session_id] = s
            merged = sorted(unique.values(), key=_session_begin)
            result[serial] = merged[-DEDUP_ID_LIMIT:]

        await self._async_save(result)
        return result
