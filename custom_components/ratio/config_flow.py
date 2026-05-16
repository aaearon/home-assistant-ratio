"""Config flow for the Ratio EV Charging integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aioratio import MemoryTokenStore, RatioClient
from aioratio.ble import parse_advertisement
from aioratio.exceptions import RatioAuthError, RatioConnectionError, RatioError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BLE_POLL_PERIOD_MAX_S,
    BLE_POLL_PERIOD_MIN_S,
    CONF_BLE_ADDRESSES,
    CONF_BLE_ENABLED_SERIALS,
    CONF_BLE_POLL_PERIODS,
    DEFAULT_BLE_POLL_PERIOD_S,
    DOMAIN,
)

# Per-charger form field keys. Static so HA translations can label them.
_FIELD_ENABLED = "enabled"
_FIELD_POLL_PERIOD = "poll_period_s"

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_credentials(hass: HomeAssistant, email: str, password: str) -> None:
    """Attempt a login. Raises RatioAuthError / RatioConnectionError on failure."""
    session = async_get_clientsession(hass)
    client = RatioClient(
        email=email,
        password=password,
        token_store=MemoryTokenStore(),
        session=session,
    )
    async with client:
        # Touching the cloud confirms the credentials work end-to-end.
        await client.chargers_overview()


class RatioConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ratio EV Charging."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None
        self._ble_serial: str | None = None
        self._ble_address: str | None = None
        self._cloud_entry_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RatioOptionsFlow:
        """Return the options flow handler."""
        return RatioOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            try:
                await _validate_credentials(self.hass, email, password)
            except RatioAuthError:
                errors["base"] = "invalid_auth"
            except RatioConnectionError:
                errors["base"] = "cannot_connect"
            except RatioError as err:
                _LOGGER.warning("Ratio API error during config flow: %s", err)
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=email,
                    data={CONF_EMAIL: email, CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-auth on auth failure."""
        entry_id = self.context.get("entry_id")
        assert entry_id is not None
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt the user to re-enter their password."""
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}
        email = self._reauth_entry.data[CONF_EMAIL]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            try:
                await _validate_credentials(self.hass, email, password)
            except RatioAuthError:
                errors["base"] = "invalid_auth"
            except RatioConnectionError:
                errors["base"] = "cannot_connect"
            except RatioError as err:
                _LOGGER.warning("Ratio API error during reauth: %s", err)
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, CONF_PASSWORD: password},
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": email},
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth discovery."""
        advert = parse_advertisement(
            discovery_info.name, discovery_info.manufacturer_data
        )
        if advert is None:
            return self.async_abort(reason="not_supported")

        # Local name is "RATIO_<serial>" — the cloud-side serial keeps the
        # "P" prefix (e.g. "P00000000013428"), so only strip "RATIO_".
        serial = advert.local_name.removeprefix("RATIO_")
        # Dedupe across rotating RPAs: every advertisement carries a new MAC,
        # so without setting unique_id we'd create one flow per advertisement.
        await self.async_set_unique_id(f"ratio_ble_{serial}")
        self._abort_if_unique_id_configured()
        self._ble_serial = serial
        self._ble_address = discovery_info.address

        # Find a loaded cloud entry that knows about this charger.
        # Limitation: when the same charger appears under two cloud accounts, the
        # first loaded entry is selected.
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if not hasattr(entry, "runtime_data"):
                continue
            try:
                chargers = entry.runtime_data.coordinator.data.chargers
            except AttributeError:
                continue
            if chargers and serial in chargers:
                self._cloud_entry_id = entry.entry_id
                break
        else:
            return self.async_abort(reason="cloud_account_required")

        if serial in entry.options.get(CONF_BLE_ENABLED_SERIALS, []):
            return self.async_abort(
                reason="ble_already_configured",
                description_placeholders={"serial": serial},
            )

        self.context["title_placeholders"] = {"name": f"Ratio Charger {serial}"}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm enabling Bluetooth for the discovered charger."""
        assert self._cloud_entry_id is not None
        assert self._ble_serial is not None
        assert self._ble_address is not None
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._cloud_entry_id)
            assert entry is not None
            existing = list(entry.options.get(CONF_BLE_ENABLED_SERIALS, []))
            if self._ble_serial not in existing:
                existing.append(self._ble_serial)
            updated_addresses = {
                **entry.options.get(CONF_BLE_ADDRESSES, {}),
                self._ble_serial: self._ble_address,
            }
            self.hass.config_entries.async_update_entry(
                entry,
                options={
                    **entry.options,
                    CONF_BLE_ENABLED_SERIALS: existing,
                    CONF_BLE_ADDRESSES: updated_addresses,
                },
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._cloud_entry_id)
            )
            return self.async_abort(
                reason="ble_configured",
                description_placeholders={"serial": self._ble_serial},
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"serial": self._ble_serial},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_mismatch(reason="account_mismatch")

            try:
                await _validate_credentials(self.hass, email, password)
            except RatioAuthError:
                errors["base"] = "invalid_auth"
            except RatioConnectionError:
                errors["base"] = "cannot_connect"
            except RatioError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reconfigure")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data={CONF_EMAIL: email, CONF_PASSWORD: password},
                )

        reconfigure_entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=reconfigure_entry.data.get(CONF_EMAIL, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class RatioOptionsFlow(OptionsFlow):
    """Handle options for the Ratio integration (BLE charger management).

    One ``charger`` substep per BLE-enabled serial — keeps the form's field
    keys static (``enabled``, ``poll_period_s``) so HA translations can
    render proper labels rather than the raw serial-suffixed keys.
    """

    def __init__(self) -> None:
        self._all_serials: list[str] = []
        self._original_periods: dict[str, float] = {}
        self._queue: list[str] = []
        self._current_serial: str | None = None
        self._enabled: list[str] = []
        self._periods: dict[str, float] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prime the per-charger walkthrough."""
        serials: list[str] = list(
            self.config_entry.options.get(CONF_BLE_ENABLED_SERIALS, [])
        )

        if not serials:
            return self.async_abort(reason="no_ble_chargers")

        self._all_serials = list(serials)
        self._original_periods = dict(
            self.config_entry.options.get(CONF_BLE_POLL_PERIODS, {})
        )
        # Start the period map from the saved values so a user who only
        # disables a charger preserves the period they previously chose;
        # re-enabling later restores it instead of silently resetting to the
        # default.
        self._periods = dict(self._original_periods)
        self._queue = list(serials)
        self._enabled = []
        return await self.async_step_charger()

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show one form per BLE-enabled serial, then finalize."""
        if user_input is not None and self._current_serial is not None:
            serial = self._current_serial
            if user_input.get(_FIELD_ENABLED, True):
                self._enabled.append(serial)
                self._periods[serial] = float(
                    user_input.get(_FIELD_POLL_PERIOD, DEFAULT_BLE_POLL_PERIOD_S)
                )
            # Disabled: leave the entry in self._periods untouched so it's
            # restored on re-enable.
            self._current_serial = None

        if not self._queue:
            disabled = [s for s in self._all_serials if s not in self._enabled]
            if disabled and hasattr(self.config_entry, "runtime_data"):
                ble_coordinators = getattr(
                    self.config_entry.runtime_data, "ble_coordinators", {}
                )
                for serial in disabled:
                    if (coord := ble_coordinators.get(serial)) is not None:
                        await coord.async_dismiss_bond_issue()
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_BLE_ENABLED_SERIALS: self._enabled,
                    CONF_BLE_POLL_PERIODS: self._periods,
                }
            )

        self._current_serial = self._queue.pop(0)
        existing = self._original_periods.get(
            self._current_serial, DEFAULT_BLE_POLL_PERIOD_S
        )
        schema = vol.Schema(
            {
                vol.Required(_FIELD_ENABLED, default=True): bool,
                vol.Required(_FIELD_POLL_PERIOD, default=existing): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=BLE_POLL_PERIOD_MIN_S, max=BLE_POLL_PERIOD_MAX_S),
                ),
            }
        )
        return self.async_show_form(
            step_id="charger",
            data_schema=schema,
            description_placeholders={"serial": self._current_serial},
        )
