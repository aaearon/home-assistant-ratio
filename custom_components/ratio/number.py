"""Number platform for Ratio EV Charging."""

from __future__ import annotations


# Note on ``# pyright: ignore[reportIncompatibleVariableOverride]`` below:
# HA's ``Entity`` base declares ``available`` (and platform classes declare
# ``is_on``/``native_value``/``options``/``current_option``/``extra_state_attributes``/etc.)
# as ``cached_property``. ``CoordinatorEntity.available`` overrides ``Entity``'s
# with a plain ``@property`` — leaving the two bases declaring the same name in
# incompatible ways. Our overrides use ``@property`` to match the dynamic
# semantics that ``CoordinatorEntity`` already relies on; using
# ``@cached_property`` here would cache values across coordinator updates and
# break tests. Official HA core integrations (fyta, reolink, snoo, etc.) use
# the same dynamic-property pattern. The variance error is structurally
# unavoidable from this side of the HA boundary.

import logging
from dataclasses import replace
from typing import Any

from aioratio import RatioClient
from aioratio.models import SolarSettings, UserSettings
from aioratio.models.settings import UpperLowerLimitSetting
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfTime
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
    """Set up Ratio numbers from a config entry."""
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
        entities: list[NumberEntity] = []
        for serial in new:
            entities.append(RatioSunOnDelayMinutesNumber(coordinator, client, serial))
            entities.append(RatioSunOffDelayMinutesNumber(coordinator, client, serial))
            entities.append(
                RatioPureSolarStartingCurrentNumber(coordinator, client, serial)
            )
            entities.append(
                RatioSmartSolarStartingCurrentNumber(coordinator, client, serial)
            )
            entities.append(
                RatioMaximumChargingCurrentNumber(coordinator, client, serial)
            )
            entities.append(
                RatioMinimumChargingCurrentNumber(coordinator, client, serial)
            )
        known.update(new)
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class _RatioNumberBase(CoordinatorEntity[RatioCoordinator], NumberEntity):
    """Common boilerplate for Ratio number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    # All current Ratio number settings are integer-valued (whole minutes,
    # whole amperes); displaying with 0 decimals avoids "6.0000001" surprises
    # when the cloud returns float-typed integers.
    _attr_suggested_display_precision = 0

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

    def _settings(self) -> Any:
        if self.coordinator.data is None:
            return None
        if self._settings_parent == "solar":
            return self.coordinator.data.solar_settings.get(self._serial)
        return self.coordinator.data.user_settings.get(self._serial)

    def _limit(self) -> UpperLowerLimitSetting | None:
        s = self._settings()
        if s is None:
            return None
        return getattr(s, self._field, None)

    # ---- properties ----

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        return super().available and self._settings() is not None

    @property
    def native_value(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        lim = self._limit()
        if lim is None:
            return None
        return lim.value

    @property
    def native_min_value(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        lim = self._limit()
        if lim is not None and lim.lower is not None:
            return lim.lower
        return self._default_min

    @property
    def native_max_value(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
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
        current: SolarSettings | None = (
            self.coordinator.data.solar_settings.get(self._serial)
            if self.coordinator.data is not None
            else None
        )
        if current is None:
            current = SolarSettings()
        existing = getattr(current, self._field, None)
        if isinstance(existing, UpperLowerLimitSetting):
            new_field = replace(existing, value=value)
        else:
            new_field = UpperLowerLimitSetting(value=value)
        match self._field:
            case "sun_on_delay_minutes":
                modified = replace(current, sun_on_delay_minutes=new_field)
            case "sun_off_delay_minutes":
                modified = replace(current, sun_off_delay_minutes=new_field)
            case "pure_solar_starting_current":
                modified = replace(current, pure_solar_starting_current=new_field)
            case "smart_solar_starting_current":
                modified = replace(current, smart_solar_starting_current=new_field)
            case _:
                raise ValueError(f"Unknown solar field: {self._field}")
        await self.coordinator.request_command(
            self._client.set_solar_settings, self._serial, modified
        )

    async def _set_user(self, value: float) -> None:
        current: UserSettings | None = (
            self.coordinator.data.user_settings.get(self._serial)
            if self.coordinator.data is not None
            else None
        )
        if current is None:
            current = UserSettings()
        existing = getattr(current, self._field, None)
        if isinstance(existing, UpperLowerLimitSetting):
            new_field = replace(existing, value=value)
        else:
            new_field = UpperLowerLimitSetting(value=value)
        match self._field:
            case "maximum_charging_current":
                modified = replace(current, maximum_charging_current=new_field)
            case "minimum_charging_current":
                modified = replace(current, minimum_charging_current=new_field)
            case _:
                raise ValueError(f"Unknown user field: {self._field}")
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
