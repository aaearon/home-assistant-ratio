"""The Ratio EV Charging integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aioratio import JsonFileTokenStore, RatioClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import RatioCoordinator
from .services import async_setup_services, async_unload_services

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Ratio integration (YAML — not supported, services only)."""
    hass.data.setdefault(DOMAIN, {})
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ratio from a config entry."""
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
    # Enter the async context manager (login + token refresh handled by client).
    await client.__aenter__()

    coordinator = RatioCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data is not None:
            client: RatioClient = data["client"]
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — closing path; never raise on unload.
                _LOGGER.debug("Error closing RatioClient", exc_info=True)
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)
    return unload_ok
