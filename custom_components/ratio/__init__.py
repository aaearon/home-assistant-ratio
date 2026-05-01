"""The Ratio EV Charging integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aioratio import JsonFileTokenStore, RatioClient
from aioratio.exceptions import RatioAuthError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import RatioCoordinator, RatioHistoryCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class RatioRuntimeData:
    """Runtime data stored in the config entry."""

    client: RatioClient
    coordinator: RatioCoordinator
    history_coordinator: RatioHistoryCoordinator


type RatioConfigEntry = ConfigEntry[RatioRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Ratio integration (YAML — not supported, services only)."""
    await async_setup_services(hass)
    return True


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

        history_coordinator = RatioHistoryCoordinator(
            hass, client, entry, coordinator
        )
        await history_coordinator.async_load()

        entry.runtime_data = RatioRuntimeData(
            client=client,
            coordinator=coordinator,
            history_coordinator=history_coordinator,
        )

        # Schedule the first history refresh as a background task — it may
        # trigger a 30-day backfill and must not block config entry setup.
        entry.async_create_background_task(
            hass,
            history_coordinator.async_config_entry_first_refresh(),
            "ratio_history_first_refresh",
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.__aexit__(None, None, None)
        raise

    return True


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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: RatioConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removal of devices that are no longer reported by the cloud."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.data is None:
        return False  # type: ignore[unreachable]
    current_serials = set(coordinator.data.chargers.keys())
    return not any(
        ident[1] in current_serials
        for ident in device_entry.identifiers
        if ident[0] == DOMAIN
    )
