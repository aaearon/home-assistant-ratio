"""Optional BLE support for the Ratio integration.

Holds a single BLE connection per charger open continuously and runs a 3 s
sensor-values poll loop (mirrors the official app's
``ChargerInformationRepository`` cadence). Each yield from
``BleClient.poll_sensor_values`` becomes a coordinator data update — entities
see voltage/current within a single advert/poll window rather than the
1-5 minute cadence that a per-poll connect/pair/disconnect produced.

Why the second ``local_name``-keyed advert subscription: the parent class
registers an *address*-keyed Bluetooth callback at ``async_start`` and never
re-keys it when ``_pick_best_device`` switches ``self.address`` to a
proxy-routed Resolvable Private Address. Proxy adverts then never wake the
session loop. A separate matcher on the stable ``RATIO_<serial>`` local name
keeps the wake path alive regardless of which scanner serves the advert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aioratio import BleClient
from aioratio.ble import parse_advertisement
from aioratio.ble.models import ChargerSensorValuesResponse
from aioratio.exceptions import (
    RatioBleConnectionError,
    RatioBleError,
    RatioBleNotBondedError,
)
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_register_callback,
    async_scanner_devices_by_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Backoff bounds for the session reconnect loop. Bond failures jump straight
# to ``_BACKOFF_BOND_S`` so the integration doesn't hammer a charger that
# requires user-side pairing intervention.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0
_BACKOFF_BOND_S = 30.0
_SESSION_TASK_CANCEL_TIMEOUT_S = 2.0
# Bounded wait on ``_wake_event`` so a missed advert callback after a transport
# drop can't park the loop indefinitely — the timeout lets the loop fall through
# to a fresh cache check (gated by ``_pick_best_device`` returning ``None`` when
# the cache is empty, so churn while genuinely offline stays bounded).
_WAKE_WAIT_TIMEOUT_S = 60.0


async def _wait_future(fut: asyncio.Future[None]) -> None:
    """Bridge ``asyncio.Future`` → coroutine so it can be wrapped in a task."""
    await fut


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


def _scanner_info(hass: HomeAssistant, address: str | None) -> tuple[str | None, bool]:
    """Return ``(scanner_source, is_remote_proxy)`` for ``address``.

    Diagnostic-only — used to disambiguate locally-attached adapter failures
    from ESPHome Bluetooth-proxy failures in log lines. ``BaseHaRemoteScanner``
    is HA's public marker for proxy-style scanners (ESPHome, Shelly, etc.);
    the returned source string is prefixed with the scanner class so reports
    name the actual backend (e.g. ``ESPHomeScanner:AA:BB:...``).
    """
    if address is None:
        return None, False
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
    """Holds a live BLE connection and pushes sensor snapshots at the configured cadence (default 3 s)."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        address: str,
        serial: str,
        poll_period_s: float,
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
        self._poll_period_s = poll_period_s
        # Wi-Fi service calls serialize against the session loop's live client
        # at the integration layer; aioratio's transaction lock then serializes
        # individual commands against the running poll.
        self._wifi_lock: asyncio.Lock = asyncio.Lock()
        self._session_task: asyncio.Task[None] | None = None
        self._client: BleClient | None = None
        # MAC of whichever advert the picker is currently using. Tracked
        # separately from ``self.address`` (which stays equal to the
        # configured address the parent's address-keyed callbacks were
        # registered against) so diagnostics + ``_scanner_info`` can name the
        # scanner actually serving the link.
        self._active_address: str | None = None
        self._local_name_unsub: CALLBACK_TYPE | None = None
        # Set whenever a fresh advert (proxy or local) is observed for
        # ``RATIO_<serial>``; cleared at the top of each session attempt so
        # the loop only re-attempts when there's evidence the charger is
        # actually nearby.
        self._wake_event: asyncio.Event = asyncio.Event()

    @property
    def available(self) -> bool:
        """Entity-facing availability tracks the live BLE transport.

        The parent ``BasePassiveBluetoothCoordinator`` flips ``self._available``
        to False when the configured BlueZ address hasn't been seen by HA's
        Bluetooth manager in ~5 min. When local hci loses the charger but an
        ESPHome BT proxy is still advertising it under a rotating RPA, the
        parent's flag goes stuck-False — entities would then read
        ``unavailable`` even though the session loop's proxy-routed link is
        live and pushing fresh snapshots at the configured cadence. Riding directly on
        ``BleClient.is_connected`` keeps availability synchronised with what
        the transport actually reports.
        """
        client = self._client
        return client is not None and client.is_connected

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start the session loop + secondary local-name advert subscription.

        The parent's ``async_start`` registers an address-keyed callback that
        is dead for proxy-routed RPAs (the address is mutated post-init by
        ``_pick_best_device``). We keep the parent registration for entity
        availability bookkeeping and layer a stable local-name matcher on
        top so the session loop wakes regardless of which scanner sees the
        next advert.
        """
        parent_stop = super().async_start()

        self._local_name_unsub = async_register_callback(
            self.hass,
            self._on_local_name_advert,
            BluetoothCallbackMatcher(
                local_name=f"RATIO_{self.serial}", connectable=True
            ),
            BluetoothScanningMode.PASSIVE,
        )

        # If any matching advert is already cached, prime the wake event so
        # the session loop doesn't sit waiting for the *next* advert when one
        # is already known. Uses the same validated predicate as
        # ``_pick_best_device`` so a name-only spoof can't prime us into
        # picking nothing.
        if self._valid_candidates():
            self._wake_event.set()

        self._session_task = self.hass.async_create_background_task(
            self._session_loop(), f"ratio_ble_session_{self.serial}"
        )

        return self._make_stop_callback(parent_stop)

    def _make_stop_callback(self, parent_stop: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Compose cleanup: cancel session task, drop local-name matcher, call parent."""

        @callback
        def _stop() -> None:
            task = self._session_task
            if task is not None and not task.done():
                task.cancel()
                # Drain the task on the HA loop so the ``finally:`` in
                # ``_session_loop`` runs (which awaits ``client.disconnect()``)
                # before we drop the local-name matcher.
                self.hass.async_create_task(
                    self._await_session_task_drained(task),
                    f"ratio_ble_session_drain_{self.serial}",
                )
            unsub = self._local_name_unsub
            if unsub is not None:
                unsub()
            self._local_name_unsub = None
            parent_stop()

        return _stop

    async def _await_session_task_drained(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(task, timeout=_SESSION_TASK_CANCEL_TIMEOUT_S)
        except (asyncio.CancelledError, TimeoutError):
            return
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "BLE session task for %s raised during shutdown",
                self.serial,
                exc_info=True,
            )

    @callback
    def _on_local_name_advert(
        self,
        _service_info: BluetoothServiceInfoBleak,
        _change: BluetoothChange,
    ) -> None:
        """Wake the session loop whenever an advert under our local name lands.

        Fires for hci0-routed and proxy-routed adverts alike (RPAs are
        irrelevant when matching on ``local_name``). The actual device pick
        happens inside ``_session_loop`` via ``_pick_best_device``.
        """
        self._wake_event.set()

    def _needs_poll(
        self,
        _service_info: BluetoothServiceInfoBleak,
        _seconds_since_last_poll: float | None,
    ) -> bool:
        """Disable the parent's debounced poll path while the session loop is alive.

        The always-on session task is the sole driver of ``self.data``; the
        parent's ``_async_poll`` would only race with it.
        """
        return self._session_task is None or self._session_task.done()

    def _valid_candidates(self) -> list[BluetoothServiceInfoBleak]:
        """All cached connectable adverts that pass the Ratio identity check.

        Shared by startup priming, the session loop's cache reprime, and
        ``_pick_best_device`` so every site uses the same predicate: matching
        ``RATIO_<serial>`` local name AND a payload under Ratio's CIC
        (``0x0BFF``) that ``parse_advertisement`` accepts. Spoofs and stale
        non-Ratio entries are filtered uniformly.
        """
        local_name = f"RATIO_{self.serial}"
        return [
            info
            for info in async_discovered_service_info(self.hass, connectable=True)
            if info.name == local_name
            and parse_advertisement(info.name, info.manufacturer_data) is not None
        ]

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

        Candidates are validated with ``parse_advertisement`` so a device
        merely spoofing the ``RATIO_<serial>`` local name (without the Ratio
        manufacturer-data payload under CIC ``0x0BFF``) cannot win on RSSI
        and receive subsequent GATT ops — notably the Wi-Fi credentials
        passed through ``reconfigure_wifi``.

        Does not mutate ``self.address``: the parent class captured the
        configured address by value and registered address-keyed callbacks
        against it at ``async_start``, so a post-init mutation would only
        mislead diagnostics. The session loop publishes the picked device's
        address to ``self._active_address`` for ``_scanner_info`` /
        diagnostics consumers.
        """
        candidates = self._valid_candidates()
        if not candidates:
            return None
        best = max(candidates, key=lambda i: i.rssi or -127)
        return best.device

    async def _async_update(
        self, _service_info: BluetoothServiceInfoBleak
    ) -> BleSnapshot:
        """No-op for the parent class — the session loop owns all I/O.

        Returns the current ``self.data`` if present, otherwise raises so the
        parent records the poll as failed. The parent never calls this while
        ``_needs_poll`` returns False, so this path is reached only if the
        session task is dead — which is exactly the right moment to surface a
        failed-update state to entities.
        """
        if self.data is not None:
            return self.data
        raise UpdateFailed("BLE session loop not active")

    async def run_wifi_command(
        self,
        fn: Callable[[BleClient], Awaitable[None]],
    ) -> None:
        """Run a Wi-Fi command against the live session client if available.

        If the session loop holds an open connection, the command is
        dispatched on it — aioratio's transaction mutex (``_send_lock``)
        serializes the command end-to-end against the running 3 s poll so
        write/response pairs cannot interleave. If no live client exists
        (the loop is in backoff), the caller falls back to a one-shot
        ``BleClient`` constructed against a fresh ``_pick_best_device``.

        Either path is gated by ``_wifi_lock`` at the caller; raises
        ``RatioBleConnectionError`` if no live client *and* no advert.
        """
        async with self._wifi_lock:
            client = self._client
            if client is not None and client.is_connected:
                await fn(client)
                return
            device = self._pick_best_device()
            if device is None:
                raise RatioBleConnectionError(
                    f"No BLE link or advert for RATIO_{self.serial}"
                )
            one_shot = BleClient(device)
            async with one_shot:
                await fn(one_shot)

    async def _session_loop(self) -> None:
        """Hold a single BleClient open and push a snapshot every poll tick.

        Re-establishes on remote disconnect, gating retries on local-name
        advert arrivals so we don't churn while the charger is genuinely
        offline. Exponential backoff (1 → 60 s) on connect/transport errors;
        bond errors jump to ``_BACKOFF_BOND_S`` and surface a repair issue.
        """
        backoff = _BACKOFF_INITIAL_S
        try:
            while True:
                # Reprime from the discovery cache before blocking. The
                # local-name advert callback may not fire after a transport
                # drop (proxy quirks, callback already debounced, etc.); if a
                # valid advert is already cached, set the wake event so the
                # loop proceeds without waiting for a fresh callback.
                if self._valid_candidates():
                    self._wake_event.set()
                # Bounded wait as a backstop: if neither the callback nor the
                # cache wakes us, fall through after the timeout so the next
                # iteration re-checks the cache. ``_pick_best_device``
                # returning ``None`` when the cache is empty keeps churn
                # bounded while the charger is genuinely offline.
                if not self._wake_event.is_set():
                    _LOGGER.debug(
                        "BLE session for %s waiting for advert (cache empty)",
                        self.serial,
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=_WAKE_WAIT_TIMEOUT_S
                    )
                self._wake_event.clear()

                device = self._pick_best_device()
                if device is None:
                    # Advert vanished between wake and pick — wait for the next.
                    continue
                self._active_address = device.address

                client = BleClient(device)
                connect_failed = False
                try:
                    await client.connect()
                    self._client = client
                    self._available = True
                    backoff = _BACKOFF_INITIAL_S
                    await self.async_dismiss_bond_issue()
                    _LOGGER.debug(
                        "BLE session established for %s via %s",
                        self.serial,
                        self._active_address,
                    )

                    disconnect_future = client.disconnect_future
                    assert disconnect_future is not None
                    disconnect_waiter = asyncio.create_task(
                        _wait_future(disconnect_future)
                    )
                    poll_task = asyncio.create_task(self._run_poll(client))
                    try:
                        done, pending = await asyncio.wait(
                            {disconnect_waiter, poll_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        # Surface any exception from the completed task so the
                        # outer ``except`` branches choose the right backoff.
                        for task in done:
                            exc = task.exception()
                            if exc is not None and not isinstance(
                                exc, asyncio.CancelledError
                            ):
                                raise exc
                    finally:
                        # Ensure subtasks are cancelled if ``asyncio.wait``
                        # itself was cancelled by the outer cancel (shutdown).
                        for task in (disconnect_waiter, poll_task):
                            if not task.done():
                                task.cancel()
                        for task in (disconnect_waiter, poll_task):
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await task
                except RatioBleNotBondedError as exc:
                    connect_failed = True
                    source, is_proxy = _scanner_info(self.hass, self._active_address)
                    _LOGGER.warning(
                        "BLE bond unrecoverable for %s (scanner=%s, proxy=%s): %s",
                        self._active_address,
                        source,
                        is_proxy,
                        exc,
                    )
                    self._fire_bond_issue()
                    backoff = _BACKOFF_BOND_S
                except (RatioBleConnectionError, RatioBleError, BleakError) as exc:
                    connect_failed = True
                    _LOGGER.debug("BLE session for %s errored: %s", self.serial, exc)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                except TimeoutError as exc:
                    connect_failed = True
                    _LOGGER.debug("BLE session for %s timed out: %s", self.serial, exc)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                finally:
                    self._client = None
                    self._available = False
                    self.async_update_listeners()
                    with contextlib.suppress(Exception):
                        await client.disconnect()

                if connect_failed:
                    await asyncio.sleep(backoff)
                # On a clean disconnect (no exception), loop right back and
                # wait for the next advert without a sleep.
        except asyncio.CancelledError:
            raise

    async def _run_poll(self, client: BleClient) -> None:
        """Drive the configured-cadence poll loop against ``client`` and push snapshots."""
        version = client.protocol_version
        async for resp in client.poll_sensor_values(period=self._poll_period_s):
            self.data = self._snapshot_from_response(resp, version)
            self.last_poll_successful = True
            self._available = True
            self.async_update_listeners()

    def _snapshot_from_response(
        self,
        resp: ChargerSensorValuesResponse,
        protocol_version: int | None,
    ) -> BleSnapshot:
        return BleSnapshot(
            serial=self.serial,
            voltage_phase_1=resp.voltage_phase_1_volts,
            voltage_phase_2=resp.voltage_phase_2_volts,
            voltage_phase_3=resp.voltage_phase_3_volts,
            current_phase_1=resp.current_phase_1_amps,
            current_phase_2=resp.current_phase_2_amps,
            current_phase_3=resp.current_phase_3_amps,
            protocol_version=protocol_version,
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
