"""Number platform for Ratio EV Charging."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from aioratio import RatioClient
from aioratio.models import SolarSettings, UserSettings
from aioratio.models.settings import UpperLowerLimitSetting

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RatioCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ratio numbers from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RatioCoordinator = data["coordinator"]
    client: RatioClient = data["client"]

    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if coordinator.data is None:
            return
        new = set(coordinator.data.chargers) - known
        if not new:
            return
        entities: list[CoordinatorEntity] = []
        for serial in new:
            entities.append(RatioSunOnDelayMinutesNumber(coordinator, client, serial))
            entities.append(RatioSunOffDelayMinutesNumber(coordinator, client, serial))
            entities.append(RatioPureSolarStartingCurrentNumber(coordinator, client, serial))
            entities.append(RatioSmartSolarStartingCurrentNumber(coordinator, client, serial))
            entities.append(RatioMaximumChargingCurrentNumber(coordinator, client, serial))
            entities.append(RatioMinimumChargingCurrentNumber(coordinator, client, serial))
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class _RatioNumberBase(CoordinatorEntity[RatioCoordinator], NumberEntity):
    """Common boilerplate for Ratio number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    # Subclasses set:
    _settings_parent: str  # "solar" or "user"
    _field: str  # attribute name on the settings dataclass
    _key: str  # unique-id / translation key

    # Default fallbacks if lower/upper are missing.
    _default_min: float = 0.0
    _default_max: float = 100.0
    _default_step: float = 1.0

    def __init__(
        self,
        coordinator: RatioCoordinator,
        client: RatioClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{serial}_{self._key}"
        self._attr_translation_key = self._key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Ratio",
            name=f"Ratio {serial}",
            serial_number=serial,
        )
        self._attr_native_step = self._default_step

    # ---- helpers ----

    def _settings(self):
        if self.coordinator.data is None:
            return None
        if self._settings_parent == "solar":
            return self.coordinator.data.solar_settings.get(self._serial)
        return self.coordinator.data.user_settings.get(self._serial)

    def _limit(self) -> Optional[UpperLowerLimitSetting]:
        s = self._settings()
        if s is None:
            return None
        return getattr(s, self._field, None)

    # ---- properties ----

    @property
    def available(self) -> bool:
        return super().available and self._settings() is not None

    @property
    def native_value(self) -> float | None:
        lim = self._limit()
        if lim is None:
            return None
        return lim.value

    @property
    def native_min_value(self) -> float:
        lim = self._limit()
        if lim is not None and lim.lower is not None:
            return lim.lower
        return self._default_min

    @property
    def native_max_value(self) -> float:
        lim = self._limit()
        if lim is not None and lim.upper is not None:
            return lim.upper
        return self._default_max

    # ---- writes ----

    async def async_set_native_value(self, value: float) -> None:
        if self._settings_parent == "solar":
            await self._set_solar(value)
        else:
            await self._set_user(value)

    async def _set_solar(self, value: float) -> None:
        current: SolarSettings | None = self.coordinator.data.solar_settings.get(
            self._serial
        ) if self.coordinator.data is not None else None
        if current is None:
            current = SolarSettings()
        existing = getattr(current, self._field, None)
        if isinstance(existing, UpperLowerLimitSetting):
            new_field = replace(existing, value=value)
        else:
            new_field = UpperLowerLimitSetting(value=value)
        modified = replace(current, **{self._field: new_field})
        await self.coordinator.request_command(
            self._client.set_solar_settings, self._serial, modified
        )

    async def _set_user(self, value: float) -> None:
        current: UserSettings | None = self.coordinator.data.user_settings.get(
            self._serial
        ) if self.coordinator.data is not None else None
        if current is None:
            current = UserSettings()
        existing = getattr(current, self._field, None)
        if isinstance(existing, UpperLowerLimitSetting):
            new_field = replace(existing, value=value)
        else:
            new_field = UpperLowerLimitSetting(value=value)
        modified = replace(current, **{self._field: new_field})
        await self.coordinator.request_command(
            self._client.set_user_settings, self._serial, modified
        )


# ---- Solar ----

class RatioSunOnDelayMinutesNumber(_RatioNumberBase):
    _settings_parent = "solar"
    _field = "sun_on_delay_minutes"
    _key = "sun_on_delay_minutes"
    _attr_name = "Sun on delay"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _default_min = 0.0
    _default_max = 60.0


class RatioSunOffDelayMinutesNumber(_RatioNumberBase):
    _settings_parent = "solar"
    _field = "sun_off_delay_minutes"
    _key = "sun_off_delay_minutes"
    _attr_name = "Sun off delay"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _default_min = 0.0
    _default_max = 60.0


class RatioPureSolarStartingCurrentNumber(_RatioNumberBase):
    _settings_parent = "solar"
    _field = "pure_solar_starting_current"
    _key = "pure_solar_starting_current"
    _attr_name = "Pure solar starting current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _default_min = 6.0
    _default_max = 32.0


class RatioSmartSolarStartingCurrentNumber(_RatioNumberBase):
    _settings_parent = "solar"
    _field = "smart_solar_starting_current"
    _key = "smart_solar_starting_current"
    _attr_name = "Smart solar starting current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _default_min = 6.0
    _default_max = 32.0


# ---- User ----

class RatioMaximumChargingCurrentNumber(_RatioNumberBase):
    _settings_parent = "user"
    _field = "maximum_charging_current"
    _key = "maximum_charging_current"
    _attr_name = "Maximum charging current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _default_min = 6.0
    _default_max = 32.0


class RatioMinimumChargingCurrentNumber(_RatioNumberBase):
    _settings_parent = "user"
    _field = "minimum_charging_current"
    _key = "minimum_charging_current"
    _attr_name = "Minimum charging current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _default_min = 6.0
    _default_max = 32.0
