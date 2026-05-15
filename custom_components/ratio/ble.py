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
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_scanner_devices_by_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
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

    def _pick_best_device(self) -> BLEDevice | None:
        """Return the BLEDevice from whichever scanner has the strongest
        current advertisement for ``RATIO_<serial>``.

        Ratio chargers rotate Resolvable Private Addresses every ~minute, so
        looking up by a single static ``self.address`` consistently returns
        the BlueZ-routed device (BlueZ alone observes the static random
        identity address). The ESPHome BT proxy sees the same charger
        through its rotating RPAs, often with a much stronger RSSI — but
        under MACs that the address-keyed lookup can never resolve. Matching
        on ``local_name`` (stable across rotation) and picking the best
        RSSI lets the coordinator use whichever scanner is actually nearest.

        Updates ``self.address`` so ``_scanner_info`` and diagnostics name
        the scanner now in use.
        """
        local_name = f"RATIO_{self.serial}"
        candidates = [
            info
            for info in async_discovered_service_info(self.hass, connectable=True)
            if info.name == local_name
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda i: i.rssi or -127)
        self.address = best.address
        return best.device

    async def _async_update(
        self, _service_info: BluetoothServiceInfoBleak
    ) -> BleSnapshot:
        device = self._pick_best_device()
        if device is None:
            raise UpdateFailed(f"No advert for RATIO_{self.serial}")

        client = BleClient(device)
        try:
            async with client:
                resp = await client.get_charger_sensor_values()
        except RatioBleNotBondedError as bond_exc:
            # aioratio>=0.10.2 already pairs-and-retries on the same connection
            # before raising this. If we still see it, the bond cannot be
            # established (charger rejected SMP, proxy lacks PAIRING feature
            # flag, etc.) — surface as a repair issue and give up the poll.
            source, is_proxy = _scanner_info(self.hass, self.address)
            _LOGGER.warning(
                "BLE bond unrecoverable for %s (scanner=%s, proxy=%s): %s",
                self.address,
                source,
                is_proxy,
                bond_exc,
            )
            self._fire_bond_issue()
            raise UpdateFailed(str(bond_exc)) from bond_exc
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

    async def async_dismiss_bond_issue(self) -> None:
        async_delete_issue(self.hass, DOMAIN, f"ble_not_bonded_{self.serial}")
