"""The Ratio EV Charging integration."""

from __future__ import annotations

import contextlib
import logging
import pathlib
from dataclasses import dataclass, field

from aioratio import JsonFileTokenStore, RatioClient
from aioratio.exceptions import RatioAuthError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ble import RatioBleCoordinator
from .const import CONF_BLE_ADDRESSES, CONF_BLE_ENABLED_SERIALS, DOMAIN, PLATFORMS
from .coordinator import RatioCoordinator, RatioHistoryCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class RatioRuntimeData:
    """Runtime data stored in the config entry."""

    client: RatioClient
    coordinator: RatioCoordinator
    history_coordinator: RatioHistoryCoordinator
    ble_coordinators: dict[str, RatioBleCoordinator] = field(default_factory=dict)


type RatioConfigEntry = ConfigEntry[RatioRuntimeData]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: RatioConfigEntry) -> bool:
    """Set up Ratio from a config entry."""
    await async_setup_services(hass)

    email: str = entry.data[CONF_EMAIL]
    password: str = entry.data[CONF_PASSWORD]

    token_path = hass.config.path(f".storage/ratio_{entry.entry_id}.tokens")
    token_store = JsonFileTokenStore(token_path)
    session = async_get_clientsession(hass)

    client = RatioClient(
        email=email,
        password=password,
        token_store=token_store,
        session=session,
    )

    try:
        await client.__aenter__()
    except RatioAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    try:
        coordinator = RatioCoordinator(hass, client, entry)
        # Load persisted preferences (preferred vehicle per charger) before the
        # first refresh so entities see the correct selection on startup.
        await coordinator.async_load_preferences()
        await coordinator.async_config_entry_first_refresh()

        history_coordinator = RatioHistoryCoordinator(hass, client, entry, coordinator)
        await history_coordinator.async_load()

        # Wire up BLE coordinators for any serials the user has enabled.
        ble_coordinators: dict[str, RatioBleCoordinator] = {}
        ble_addresses: dict[str, str] = entry.options.get(CONF_BLE_ADDRESSES, {})
        for serial in entry.options.get(CONF_BLE_ENABLED_SERIALS, []):
            address = ble_addresses.get(serial)
            if address is None:
                _LOGGER.warning(
                    "BLE address unknown for charger %s; skipping BLE setup", serial
                )
                continue
            ble_coord = RatioBleCoordinator(
                hass=hass,
                logger=_LOGGER,
                address=address,
                serial=serial,
            )
            # async_start() subscribes to BT events; the returned cancel callback
            # is registered with async_on_unload so cleanup is automatic.
            entry.async_on_unload(ble_coord.async_start())
            ble_coordinators[serial] = ble_coord

        entry.runtime_data = RatioRuntimeData(
            client=client,
            coordinator=coordinator,
            history_coordinator=history_coordinator,
            ble_coordinators=ble_coordinators,
        )

        # Schedule the first history refresh as a background task — it may
        # trigger a 30-day backfill and must not block config entry setup.
        entry.async_create_background_task(
            hass,
            history_coordinator.async_config_entry_first_refresh(),
            "ratio_history_first_refresh",
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Reload the entry whenever options change so BLE coordinator list
        # stays in sync with the user's choices.
        entry.async_on_unload(
            entry.add_update_listener(_async_reload_on_options_change)
        )
    except Exception:
        await client.__aexit__(None, None, None)
        raise

    return True


async def _async_reload_on_options_change(
    hass: HomeAssistant, entry: RatioConfigEntry
) -> None:
    """Reload the entry when options change (e.g. BLE serial list updated)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RatioConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            await entry.runtime_data.client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 — closing path; never raise on unload.
            _LOGGER.debug("Error closing RatioClient", exc_info=True)
        if len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
            await async_unload_services(hass)
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: RatioConfigEntry
) -> None:
    """Delete the per-entry token store when the integration is removed."""
    token_path = pathlib.Path(
        hass.config.path(f".storage/ratio_{entry.entry_id}.tokens")
    )

    def _unlink() -> None:
        with contextlib.suppress(FileNotFoundError):
            token_path.unlink()

    await hass.async_add_executor_job(_unlink)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current schema.

    Only ``VERSION = 1`` has ever shipped, so no entries from earlier
    versions exist in the wild. We still bump anything that claims a
    lower version number to 1 so the migration framework records progress
    and HA does not loop calling this function on every restart. When
    schema v2 ships, add an explicit ``if entry.version == 1:`` branch
    above the down-migration guard that updates the data and bumps the
    version number.
    """
    if entry.version > 1:
        # Down-migration: refuse so HA surfaces a setup error instead of
        # silently corrupting newer data.
        _LOGGER.error(
            "Cannot downgrade Ratio config entry %s from v%s to v1",
            entry.entry_id,
            entry.version,
        )
        return False
    if entry.version < 1:
        # Defensive: should never appear in the wild. Bump forward so HA
        # records the migration as complete and stops re-invoking us.
        _LOGGER.debug(
            "Bumping Ratio config entry %s from v%s to v1 (no data changes)",
            entry.entry_id,
            entry.version,
        )
        hass.config_entries.async_update_entry(entry, version=1)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: RatioConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal of devices that are no longer reported by the cloud."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.data is None:
        return False
    ratio_serials = {
        ident[1] for ident in device_entry.identifiers if ident[0] == DOMAIN
    }
    if not ratio_serials:
        return False
    current_serials = set(coordinator.data.chargers.keys())
    return not any(serial in current_serials for serial in ratio_serials)
