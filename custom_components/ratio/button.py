"""Button platform for Ratio EV Charging."""
from __future__ import annotations

import logging

from aioratio import RatioClient
from aioratio.models import ChargerOverview

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import EntityCategory
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
    """Set up Ratio buttons from a config entry."""
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
        entities: list[ButtonEntity] = [
            RatioGrantUpgradePermissionButton(coordinator, client, serial)
            for serial in new
        ]
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class RatioGrantUpgradePermissionButton(
    CoordinatorEntity[RatioCoordinator], ButtonEntity
):
    """Button to grant permission to apply queued firmware update jobs."""

    _attr_has_entity_name = True
    _attr_translation_key = "grant_upgrade_permission"
    _attr_name = "Grant upgrade permission"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{serial}_grant_upgrade_permission"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )

    @property
    def _overview(self) -> ChargerOverview | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.chargers.get(self._serial)

    def _job_ids(self) -> list[str]:
        ov = self._overview
        if ov is None or ov.charger_firmware_status is None:
            return []
        return [
            j.job_id
            for j in ov.charger_firmware_status.firmware_update_jobs
            if j.job_id
        ]

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        ov = self._overview
        if ov is None or ov.charger_firmware_status is None:
            return False
        fw = ov.charger_firmware_status
        if not fw.is_firmware_update_available:
            return False
        if fw.is_firmware_update_allowed:
            return False
        return len(self._job_ids()) > 0

    async def async_press(self) -> None:
        job_ids = self._job_ids()
        if not job_ids:
            _LOGGER.debug(
                "grant_upgrade_permission: no firmware_update_jobs for %s",
                self._serial,
            )
            return
        await self.coordinator.request_command(
            self._client.grant_upgrade_permission,
            self._serial,
            firmware_update_job_ids=job_ids,
        )
