"""Text platform for Ratio EV Charging."""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from aioratio import RatioClient
from aioratio.models import InstallerOcppSettings

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RatioConfigEntry
from .const import DOMAIN
from .coordinator import RatioCoordinator

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RatioConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio text entities from a config entry."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities = [
            RatioChargePointIdentifierText(coordinator, client, serial)
            for serial in new
        ]
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class RatioChargePointIdentifierText(CoordinatorEntity[RatioCoordinator], TextEntity):
    """Text entity for setting the OCPP charge point identifier."""

    _attr_has_entity_name = True
    _attr_translation_key = "charge_point_identifier"
    _attr_name = "Charge point identifier"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{serial}_charge_point_identifier_text"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )

    def _ocpp_settings(self) -> InstallerOcppSettings | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.ocpp_settings.get(self._serial)

    @property
    def available(self) -> bool:
        settings = self._ocpp_settings()
        if settings is None:
            return False
        return settings.charge_point_identifier_status.is_change_allowed

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        settings = self._ocpp_settings()
        if settings is None:
            return None
        reason = settings.charge_point_identifier_status.change_not_allowed_reason
        if reason is not None:
            return {"change_not_allowed_reason": reason}
        return None

    @property
    def native_max(self) -> int:
        settings = self._ocpp_settings()
        if settings is not None and settings.charge_point_identifier_max_length is not None:
            return settings.charge_point_identifier_max_length
        return 255

    @property
    def native_value(self) -> str | None:
        settings = self._ocpp_settings()
        if settings is None:
            return None
        return settings.charge_point_identifier

    async def async_set_value(self, value: str) -> None:
        settings = self._ocpp_settings() or InstallerOcppSettings()
        await self.coordinator.request_command(
            self._client.set_ocpp_settings,
            self._serial,
            dataclasses.replace(settings, charge_point_identifier=value),
        )
