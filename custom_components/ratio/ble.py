"""Optional BLE support for the Ratio integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aioratio import BleClient
from aioratio.exceptions import (
    RatioBleConnectionError,
    RatioBleError,
    RatioBleNotBondedError,
)
from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_scanner_devices_by_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.components.bluetooth.api import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class BleSnapshot:
    serial: str
    voltage_phase_1: float | None  # volts
    voltage_phase_2: float | None
    voltage_phase_3: float | None
    current_phase_1: float | None  # amps
    current_phase_2: float | None
    current_phase_3: float | None
    protocol_version: int | None


def _scanner_info(hass: HomeAssistant, address: str) -> tuple[str | None, bool]:
    """Return ``(scanner_source, is_remote_proxy)`` for ``address``.

    Diagnostic-only — used to disambiguate locally-attached adapter failures
    from ESPHome Bluetooth-proxy failures in log lines. ``BaseHaRemoteScanner``
    is HA's public marker for proxy-style scanners (ESPHome, Shelly, etc.);
    the returned source string is prefixed with the scanner class so reports
    name the actual backend (e.g. ``ESPHomeScanner:AA:BB:...``).

    Returns ``(None, False)`` if no scanner is currently providing the device,
    which is itself a useful diagnostic. Any unexpected failure inside the BT
    manager is logged at DEBUG so a stale "scanner=None" line in a report can
    be distinguished from "the lookup itself blew up".
    """
    try:
        devices = async_scanner_devices_by_address(hass, address, connectable=True)
    except Exception as exc:  # noqa: BLE001 — diagnostic helper, must never raise
        _LOGGER.debug(
            "_scanner_info lookup for %s failed: %s: %s",
            address,
            type(exc).__name__,
            exc,
        )
        return None, False
    if not devices:
        return None, False
    scanner = devices[0].scanner
    source = f"{type(scanner).__name__}:{scanner.source}"
    return source, isinstance(scanner, BaseHaRemoteScanner)


class RatioBleCoordinator(ActiveBluetoothDataUpdateCoordinator[BleSnapshot]):
    """Polls a Ratio charger over BLE and exposes a BleSnapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        address: str,
        serial: str,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=BluetoothScanningMode.PASSIVE,
            connectable=True,
        )
        self.serial = serial
        self._wifi_lock: asyncio.Lock = asyncio.Lock()

    def _needs_poll(
        self,
        _service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        return seconds_since_last_poll is None or seconds_since_last_poll >= 45

    async def _async_update(
        self, _service_info: BluetoothServiceInfoBleak
    ) -> BleSnapshot:
        device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise UpdateFailed("Device not found")

        client = BleClient(device)
        try:
            async with client:
                resp = await client.get_charger_sensor_values()
        except RatioBleNotBondedError as bond_exc:
            # Log scanner routing so reporters can tell whether the failure
            # came via a local BlueZ adapter or an ESPHome BT proxy — the two
            # paths have different pairing semantics.
            source, is_proxy = _scanner_info(self.hass, self.address)
            _LOGGER.debug(
                "BLE bond-required for %s (scanner=%s, proxy=%s): %s",
                self.address,
                source,
                is_proxy,
                bond_exc,
            )
            # Try OS-level bonding once (Ember Mug pattern). BlueZ stores the
            # bond so subsequent connections skip this branch entirely.
            if await self._try_pair():
                try:
                    device = async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    )
                    if device is None:
                        # Device went out of range between pairing and re-fetch.
                        raise UpdateFailed(
                            f"Charger {self.serial} not advertising after pairing"
                        )
                    client = BleClient(device)
                    async with client:
                        resp = await client.get_charger_sensor_values()
                except (
                    RatioBleNotBondedError,
                    RatioBleConnectionError,
                    RatioBleError,
                    BleakError,
                ) as e:
                    self._fire_bond_issue()
                    raise UpdateFailed(str(e)) from e
            else:
                self._fire_bond_issue()
                raise UpdateFailed(
                    f"Charger {self.serial} is not bonded and pairing failed"
                ) from None
        except (RatioBleConnectionError, RatioBleError) as e:
            raise UpdateFailed(str(e)) from e
        except BleakError as e:
            raise UpdateFailed(str(e)) from e

        return BleSnapshot(
            serial=self.serial,
            voltage_phase_1=resp.voltage_phase_1_volts,
            voltage_phase_2=resp.voltage_phase_2_volts,
            voltage_phase_3=resp.voltage_phase_3_volts,
            current_phase_1=resp.current_phase_1_amps,
            current_phase_2=resp.current_phase_2_amps,
            current_phase_3=resp.current_phase_3_amps,
            protocol_version=client.protocol_version,
        )

    def _fire_bond_issue(self) -> None:
        async_create_issue(
            self.hass,
            DOMAIN,
            f"ble_not_bonded_{self.serial}",
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key="ble_not_bonded",
            translation_placeholders={"serial": self.serial},
        )

    async def _try_pair(self) -> bool:
        """Attempt OS-level bonding via bleak. Returns True if successful.

        BlueZ persists the bond to disk; once bonded, the charger's ATT
        authentication check passes and subsequent connections skip this path.
        ESPHome BT proxies expose ``pair()`` via the PAIRING feature flag
        (firmware 2024.6+, ``active: true``); when the proxy lacks that flag
        ``bleak`` raises ``NotImplementedError``.
        """
        source, is_proxy = _scanner_info(self.hass, self.address)
        _LOGGER.debug(
            "Attempting BLE pair for %s via scanner=%s (proxy=%s)",
            self.address,
            source,
            is_proxy,
        )
        device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            _LOGGER.warning(
                "BLE pair for %s aborted: device not advertising "
                "(scanner=%s, proxy=%s)",
                self.address,
                source,
                is_proxy,
            )
            return False
        try:
            async with BleakClient(device, timeout=15) as raw:
                # bleak >=1.0 made pair() return None — failures raise. The
                # bleak_esphome backend's bool return value is swallowed by
                # the wrapper, so we cannot detect a proxy-reported
                # paired=False from here. That branch logs at ERROR inside
                # ``bleak_esphome.backend.client`` directly; surface it by
                # enabling DEBUG for that logger.
                await raw.pair()
        except NotImplementedError as exc:
            _LOGGER.warning(
                "BLE pair for %s raised NotImplementedError "
                "(scanner=%s, proxy=%s): %s. ESPHome BT proxies need "
                "firmware 2024.6+ with `bluetooth_proxy: active: true`.",
                self.address,
                source,
                is_proxy,
                exc,
            )
            return False
        except (BleakError, TimeoutError, OSError) as exc:
            _LOGGER.warning(
                "BLE pair for %s failed (scanner=%s, proxy=%s): %s: %s",
                self.address,
                source,
                is_proxy,
                type(exc).__name__,
                exc,
            )
            _LOGGER.debug("BLE pair traceback for %s", self.address, exc_info=exc)
            return False
        return True

    async def async_dismiss_bond_issue(self) -> None:
        async_delete_issue(self.hass, DOMAIN, f"ble_not_bonded_{self.serial}")
