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
import math
from typing import Any

from aioratio import RatioClient
from aioratio.models import SolarSettingsUpdate, UserSettingsUpdate
from aioratio.models.settings import UpperLowerLimitSetting
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RatioConfigEntry
from .const import DOMAIN
from .coordinator import RatioCoordinator

PARALLEL_UPDATES = 1


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

    # Display-only fallbacks for the frontend slider when lower/upper are
    # missing. HA types native_min_value/native_max_value as plain ``float``,
    # so there is no way to say "unknown" there. These are NOT charger limits
    # and must never be used to validate a write — see ``_bounds()``.
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

    def _bounds(self) -> tuple[float, float] | None:
        """Return the charger-reported ``(lower, upper)`` pair, or ``None``.

        This is the only bounds source the write path may use. The
        ``_default_min``/``_default_max`` class constants are display
        scaffolding, not charger limits — the reference charger reports an
        ``upperLimit`` of 16 for the solar starting currents while the
        constant says 32 — so validating a write against them would accept
        values the cloud never would.
        """
        lim = self._limit()
        if lim is None or lim.lower is None or lim.upper is None:
            return None
        return lim.lower, lim.upper

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

    def _validate(self, value: float) -> int:
        """Return ``value`` as an ``int``, or raise ``HomeAssistantError``.

        Every cloud PUT field behind these entities is typed ``Int?`` in the
        Kotlin serializers, so a fractional or non-finite value cannot be
        represented on the wire at all. Those two checks deliberately do
        **not** consult the coordinator cache: with an empty cache the old
        code skipped integrality entirely and let a float reach the API.

        The range check is fail-closed instead. It needs the charger's own
        ``lowerLimit``/``upperLimit``, so when the settings descriptor (or
        either bound) is missing the write is refused rather than checked
        against the display fallbacks.
        """
        if not math.isfinite(value):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="number_value_not_finite",
                translation_placeholders={"setting": self._key, "value": str(value)},
            )
        if not float(value).is_integer():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="number_value_not_integer",
                translation_placeholders={"setting": self._key, "value": str(value)},
            )
        as_int = int(value)
        bounds = self._bounds()
        if bounds is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="number_bounds_unknown",
                translation_placeholders={"setting": self._key, "value": str(as_int)},
            )
        minimum, maximum = bounds
        if as_int < minimum or as_int > maximum:
            # Refuse rather than clamp: an external controller has to be told
            # its request was not applied.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="number_value_out_of_range",
                translation_placeholders={
                    "setting": self._key,
                    "value": str(as_int),
                    "minimum": str(minimum),
                    "maximum": str(maximum),
                },
            )
        return as_int

    async def async_set_native_value(self, value: float) -> None:
        validated = self._validate(value)
        if self._settings_parent == "solar":
            await self._set_solar(validated)
        else:
            await self._set_user(validated)

    async def _set_solar(self, value: int) -> None:
        """PUT only the key this entity owns.

        ``SetSolarSettings$$serializer.java`` declares all four elements
        optional and nullable, and the app omits everything the current screen
        did not change. Sending the whole cached document instead re-asserts
        stale values and races other writers.
        """
        match self._field:
            case "sun_on_delay_minutes":
                update = SolarSettingsUpdate(sun_on_delay_minutes=value)
            case "sun_off_delay_minutes":
                update = SolarSettingsUpdate(sun_off_delay_minutes=value)
            case "pure_solar_starting_current":
                update = SolarSettingsUpdate(pure_solar_starting_current=value)
            case "smart_solar_starting_current":
                update = SolarSettingsUpdate(smart_solar_starting_current=value)
            case _:
                raise ValueError(f"Unknown solar field: {self._field}")
        await self.coordinator.request_command(
            self._client.set_solar_settings, self._serial, update
        )

    async def _set_user(self, value: int) -> None:
        """PUT only the key this entity owns (``SetUserSettings$$serializer``)."""
        match self._field:
            case "maximum_charging_current":
                update = UserSettingsUpdate(maximum_charging_current=value)
            case "minimum_charging_current":
                update = UserSettingsUpdate(minimum_charging_current=value)
            case _:
                raise ValueError(f"Unknown user field: {self._field}")
        await self.coordinator.request_command(
            self._client.set_user_settings, self._serial, update
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
