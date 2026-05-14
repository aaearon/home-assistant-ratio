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
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
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
        except RatioBleNotBondedError:
            # Try OS-level bonding once (Ember Mug pattern). BlueZ stores the
            # bond so subsequent connections skip this branch entirely.
            if await self._try_pair():
                try:
                    device = async_ble_device_from_address(
                        self.hass, self.address, connectable=True
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
        """
        device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            return False
        try:
            async with BleakClient(device, timeout=15) as raw:
                await raw.pair()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def async_dismiss_bond_issue(self) -> None:
        async_delete_issue(self.hass, DOMAIN, f"ble_not_bonded_{self.serial}")
