"""Tests for RatioBleCoordinator and BleSnapshot.

The homeassistant.components.bluetooth package transitively imports
``serial`` (via usb), which is not installed in the test environment.
We stub it out at the top of this module before any HA bluetooth import.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub out pyserial before any HA bluetooth submodule is imported.
# ---------------------------------------------------------------------------
def _stub_serial() -> None:
    if "serial" in sys.modules:
        return
    serial_mod = ModuleType("serial")
    tools_mod = ModuleType("serial.tools")
    list_ports_mod = ModuleType("serial.tools.list_ports")
    list_ports_common_mod = ModuleType("serial.tools.list_ports_common")

    list_ports_mod.comports = lambda: []  # type: ignore[attr-defined]
    list_ports_common_mod.ListPortInfo = object  # type: ignore[attr-defined]

    serial_mod.tools = tools_mod  # type: ignore[attr-defined]
    tools_mod.list_ports = list_ports_mod  # type: ignore[attr-defined]
    tools_mod.list_ports_common = list_ports_common_mod  # type: ignore[attr-defined]

    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools_mod
    sys.modules["serial.tools.list_ports"] = list_ports_mod
    sys.modules["serial.tools.list_ports_common"] = list_ports_common_mod


_stub_serial()

# ---------------------------------------------------------------------------
# Now we can safely import bluetooth-dependent HA modules.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import contextlib  # noqa: E402

from aioratio.ble.models.sensors import ChargerSensorValuesResponse  # noqa: E402
from aioratio.exceptions import (  # noqa: E402
    RatioBleConnectionError,
    RatioBleNotBondedError,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.issue_registry import IssueSeverity  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.ratio.const import DOMAIN  # noqa: E402

if TYPE_CHECKING:
    from custom_components.ratio.ble import RatioBleCoordinator


def _make_sensor_response(
    v1=2300, v2=2310, v3=2290, a1=160, a2=None, a3=None
) -> ChargerSensorValuesResponse:
    return ChargerSensorValuesResponse(
        transaction="t1",
        result="ok",
        actual_mains_voltage_phase_1=v1,
        actual_mains_voltage_phase_2=v2,
        actual_mains_voltage_phase_3=v3,
        actual_sensor_box_current_phase_1=a1,
        actual_sensor_box_current_phase_2=a2,
        actual_sensor_box_current_phase_3=a3,
    )


def _make_ble_client_mock(sensor_response: ChargerSensorValuesResponse) -> MagicMock:
    """Mock BleClient that yields a single sensor response from poll_sensor_values."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_charger_sensor_values = AsyncMock(return_value=sensor_response)
    client.protocol_version = 2
    client.is_connected = True

    async def _poll(period: float = 3.0):
        yield sensor_response

    client.poll_sensor_values = _poll
    # disconnect_future is awaited by _session_loop alongside the poll task.
    # A never-resolving future means the poll task will be the one that
    # finishes (after exhausting the single yield) and exit the wait.
    loop = asyncio.get_event_loop()
    client.disconnect_future = loop.create_future()
    return client


def _make_service_info(
    address: str = "AA:BB:CC:DD:EE:FF",
    *,
    name: str | None = "RATIO_SN001",
    rssi: int = -70,
    source: str = "00:11:22:33:44:55",
    manufacturer_data: dict[int, bytes] | None = None,
) -> MagicMock:
    """Build a fake BluetoothServiceInfoBleak.

    Defaults match the canonical test serial ``SN001`` so the coordinator's
    ``RATIO_{serial}`` matcher accepts it. ``manufacturer_data`` defaults to a
    valid Ratio payload (``ADVERT_MANUFACTURER_ID = 0x0BFF``) so candidates
    pass ``parse_advertisement`` — pass ``{}`` (or a different ID) to simulate
    a spoofed advert that should be rejected by the picker.
    """
    info = MagicMock()
    info.address = address
    info.name = name
    info.rssi = rssi
    info.source = source
    info.device = MagicMock(name=f"BLEDevice({address})")
    info.connectable = True
    info.manufacturer_data = (
        {0x0BFF: b"\x03"} if manufacturer_data is None else manufacturer_data
    )
    return info


def _make_coordinator(hass: HomeAssistant) -> RatioBleCoordinator:
    """Build a RatioBleCoordinator with the parent __init__ stubbed out."""
    import logging

    from homeassistant.components.bluetooth.active_update_coordinator import (
        ActiveBluetoothDataUpdateCoordinator,
    )

    from custom_components.ratio.ble import RatioBleCoordinator

    def _fake_parent_init(self, *a, **kw) -> None:
        # Provide the minimal attributes the parent expects so methods work.
        self.hass = hass
        self.address = "AA:BB:CC:DD:EE:FF"
        self.logger = logging.getLogger(__name__)

    with patch.object(
        ActiveBluetoothDataUpdateCoordinator, "__init__", _fake_parent_init
    ):
        coord = RatioBleCoordinator(
            hass=hass,
            logger=logging.getLogger(__name__),
            address="AA:BB:CC:DD:EE:FF",
            serial="SN001",
        )
    return coord


def _stub_coordinator_for_session(coord) -> None:
    """Wire the minimum attributes ``_session_loop`` reads from the parent.

    The parent ``ActiveBluetoothDataUpdateCoordinator``'s ``__init__`` is
    stubbed by ``_make_coordinator``, so these attributes are set explicitly
    here for tests that exercise the session loop.
    """
    coord.data = None
    coord._available = False
    coord.last_poll_successful = True
    coord._listeners = {}


@pytest.mark.asyncio
async def test_run_poll_pushes_snapshot_to_self_data(hass: HomeAssistant) -> None:
    """``_run_poll`` writes a scaled ``BleSnapshot`` to ``self.data`` per yield."""
    from custom_components.ratio.ble import BleSnapshot

    sensor_resp = _make_sensor_response()
    coord = _make_coordinator(hass)
    _stub_coordinator_for_session(coord)

    listeners: list[None] = []
    coord.async_update_listeners = MagicMock(side_effect=lambda: listeners.append(None))

    client = _make_ble_client_mock(sensor_resp)
    await coord._run_poll(client)

    assert isinstance(coord.data, BleSnapshot)
    assert coord.data.serial == "SN001"
    assert coord.data.voltage_phase_1 == 230.0
    assert coord.data.voltage_phase_2 == 231.0
    assert coord.data.voltage_phase_3 == 229.0
    assert coord.data.current_phase_1 == 16.0
    assert coord.data.protocol_version == 2
    assert coord._available is True
    # Listeners notified at least once per yield.
    assert listeners


@pytest.mark.asyncio
async def test_session_loop_fires_bond_issue(hass: HomeAssistant) -> None:
    """``RatioBleNotBondedError`` from ``connect()`` fires the repair issue.

    Drives the loop just long enough to execute the connect → except branch,
    then cancels while it parks in its bond backoff sleep.
    """
    coord = _make_coordinator(hass)
    _stub_coordinator_for_session(coord)
    coord._wake_event.set()

    bond_client = MagicMock()
    bond_client.connect = AsyncMock(side_effect=RatioBleNotBondedError("not bonded"))
    bond_client.disconnect = AsyncMock()
    bond_client.is_connected = False

    issue_fired = asyncio.Event()

    def _on_create_issue(*args, **kwargs) -> None:
        issue_fired.set()

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=MagicMock(),
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=bond_client),
        patch(
            "custom_components.ratio.ble.async_create_issue",
            side_effect=_on_create_issue,
        ) as mock_create_issue,
    ):
        task = asyncio.create_task(coord._session_loop())
        try:
            await asyncio.wait_for(issue_fired.wait(), timeout=1.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    mock_create_issue.assert_called_with(
        hass,
        DOMAIN,
        "ble_not_bonded_SN001",
        is_fixable=False,
        severity=IssueSeverity.ERROR,
        translation_key="ble_not_bonded",
        translation_placeholders={"serial": "SN001"},
    )


@pytest.mark.asyncio
async def test_session_loop_backoff_on_connection_error(hass: HomeAssistant) -> None:
    """``RatioBleConnectionError`` from connect grows the backoff exponentially.

    Watches the value of ``asyncio.sleep``'s ``delay`` argument across the
    first two iterations; first should be ``initial * 2`` (=2.0), second
    ``initial * 4`` (=4.0). Pre-seeds the wake event between attempts so the
    loop progresses without us having to manually nudge it.
    """
    coord = _make_coordinator(hass)
    _stub_coordinator_for_session(coord)
    coord._wake_event.set()

    broken_client = MagicMock()
    broken_client.connect = AsyncMock(side_effect=RatioBleConnectionError("nope"))
    broken_client.disconnect = AsyncMock()
    broken_client.is_connected = False

    sleeps: list[float] = []
    second_sleep = asyncio.Event()

    real_sleep = asyncio.sleep

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)
        # Re-wake immediately so the next iteration runs.
        coord._wake_event.set()
        if len(sleeps) >= 2:
            second_sleep.set()
        # Yield once so other tasks (including the test waiter) get a turn
        # without paying the actual backoff cost.
        await real_sleep(0)

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=MagicMock(),
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=broken_client),
        patch("custom_components.ratio.ble.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(coord._session_loop())
        try:
            await asyncio.wait_for(second_sleep.wait(), timeout=1.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert sleeps[0] == 2.0, sleeps
    assert sleeps[1] == 4.0, sleeps


@pytest.mark.asyncio
async def test_session_loop_returns_data_via_run_poll(hass: HomeAssistant) -> None:
    """End-to-end: a wake → connect → poll → snapshot pushed to ``self.data``."""
    from custom_components.ratio.ble import BleSnapshot

    sensor_resp = _make_sensor_response()
    client = _make_ble_client_mock(sensor_resp)

    snapshot_set = asyncio.Event()

    async def _poll(period: float = 3.0):
        yield sensor_resp
        snapshot_set.set()
        # Hang so the disconnect_future is the one that finishes the wait.
        await asyncio.Event().wait()

    client.poll_sensor_values = _poll

    coord = _make_coordinator(hass)
    _stub_coordinator_for_session(coord)
    coord._wake_event.set()

    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=MagicMock(),
        ),
        patch("custom_components.ratio.ble.BleClient", return_value=client),
    ):
        task = asyncio.create_task(coord._session_loop())
        try:
            await asyncio.wait_for(snapshot_set.wait(), timeout=1.0)
            # Give the next scheduler tick to flush ``self.data`` from the
            # poll iteration that just yielded the sentinel.
            await asyncio.sleep(0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert isinstance(coord.data, BleSnapshot)
    assert coord.data.serial == "SN001"


def test_scanner_info_detects_remote_proxy_scanner() -> None:
    """_scanner_info reports proxy=True for a BaseHaRemoteScanner."""
    from homeassistant.components.bluetooth import BaseHaRemoteScanner

    from custom_components.ratio.ble import _scanner_info

    proxy_scanner = MagicMock(spec=BaseHaRemoteScanner)
    proxy_scanner.source = "AA:BB:CC:DD:EE:00"
    scanner_dev = MagicMock()
    scanner_dev.scanner = proxy_scanner

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    # Source format is ``<scanner-class>:<scanner.source>`` so reporters
    # can tell ESPHomeScanner from ShellyScanner without extra log digging.
    assert source is not None
    assert source.endswith(":AA:BB:CC:DD:EE:00")
    assert is_proxy is True


def test_scanner_info_returns_local_scanner_as_non_proxy() -> None:
    """A local (BlueZ) scanner is not a BaseHaRemoteScanner, so proxy=False."""
    from custom_components.ratio.ble import _scanner_info

    local_scanner = MagicMock()
    local_scanner.source = "hci0"
    scanner_dev = MagicMock()
    scanner_dev.scanner = local_scanner

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[scanner_dev],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is not None
    assert source.endswith(":hci0")
    assert is_proxy is False


def test_scanner_info_handles_no_scanner_for_address() -> None:
    """If no scanner is currently providing the device, return (None, False)."""
    from custom_components.ratio.ble import _scanner_info

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        return_value=[],
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is None
    assert is_proxy is False


def test_scanner_info_swallows_api_errors() -> None:
    """The helper is diagnostic and must never raise — even on API errors."""
    from custom_components.ratio.ble import _scanner_info

    with patch(
        "custom_components.ratio.ble.async_scanner_devices_by_address",
        side_effect=RuntimeError("bt manager not ready"),
    ):
        source, is_proxy = _scanner_info(MagicMock(), "AA:BB:CC:DD:EE:FF")

    assert source is None
    assert is_proxy is False


def test_needs_poll_false_while_session_task_alive(hass: HomeAssistant) -> None:
    """``_needs_poll`` returns False whenever the session loop is alive.

    The always-on session is the sole driver of ``self.data`` — the parent's
    debounced poll path would only race with it. The fallback (return True)
    only matters when the session task has died.
    """
    coord = _make_coordinator(hass)
    service_info = MagicMock()

    alive_task = MagicMock()
    alive_task.done.return_value = False
    coord._session_task = alive_task
    assert coord._needs_poll(service_info, None) is False
    assert coord._needs_poll(service_info, 100.0) is False

    dead_task = MagicMock()
    dead_task.done.return_value = True
    coord._session_task = dead_task
    assert coord._needs_poll(service_info, None) is True

    coord._session_task = None
    assert coord._needs_poll(service_info, None) is True


# ---------------------------------------------------------------------------
# _pick_best_device — name-based scanner selection.
#
# Reason for the refactor: the integration originally locked
# ``coordinator.address`` to the address discovered at config-entry creation
# time. Ratio chargers rotate Resolvable Private Addresses every ~minute, so
# an ESPHome BT-proxy (which sees only RPAs) and the local BlueZ adapter
# (which observes the static random identity address) report the same
# charger under different MACs. Looking up by ``self.address`` always
# resolved to whichever scanner happened to see that one address — usually
# the weaker one. ``_pick_best_device`` walks every connectable advert,
# filters by ``RATIO_<serial>`` (stable across rotation), and picks the
# strongest RSSI so the coordinator routes through whichever scanner is
# actually closest to the charger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_best_device_returns_strongest_rssi(hass: HomeAssistant) -> None:
    """When multiple scanners see the charger, pick the strongest-RSSI advert."""
    weak = _make_service_info(
        address="C0:49:EF:F5:AA:FE", rssi=-86, source="40:23:43:1D:C8:EC"
    )
    strong = _make_service_info(
        address="79:75:75:A4:A0:45", rssi=-59, source="F4:2D:C9:70:C2:7E"
    )
    unrelated = _make_service_info(address="BC:10:2F:20:DE:13", name="Fridge", rssi=-50)
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[weak, unrelated, strong],
    ):
        device = coord._pick_best_device()

    assert device is strong.device
    # self.address is updated so diagnostics + _scanner_info reflect the
    # scanner actually being used.
    assert coord.address == "79:75:75:A4:A0:45"


@pytest.mark.asyncio
async def test_pick_best_device_returns_none_when_no_match(
    hass: HomeAssistant,
) -> None:
    """If no advert matches ``RATIO_<serial>``, return None — caller raises."""
    unrelated = _make_service_info(address="BC:10:2F:20:DE:13", name="Fridge", rssi=-50)
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[unrelated],
    ):
        device = coord._pick_best_device()

    assert device is None


@pytest.mark.asyncio
async def test_pick_best_device_rejects_name_only_spoof(hass: HomeAssistant) -> None:
    """A device advertising ``RATIO_<serial>`` without Ratio manufacturer data
    must not be selected. Otherwise any nearby device spoofing the local name
    could win by RSSI and receive subsequent GATT ops, including Wi-Fi
    credentials via ``reconfigure_wifi``."""
    spoof = _make_service_info(
        address="DE:AD:BE:EF:00:00",
        rssi=-30,  # very strong, but should be ignored
        manufacturer_data={},  # no manufacturer data at all
    )
    real = _make_service_info(
        address="79:75:75:A4:A0:45",
        rssi=-72,
        manufacturer_data={0x0BFF: b"\x03"},
    )
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[spoof, real],
    ):
        device = coord._pick_best_device()

    assert device is real.device
    assert coord.address == "79:75:75:A4:A0:45"


@pytest.mark.asyncio
async def test_pick_best_device_rejects_wrong_manufacturer_id(
    hass: HomeAssistant,
) -> None:
    """A spoof carrying *some* manufacturer data under a non-Ratio company ID
    must also be rejected — the Ratio CIC is ``0x0BFF`` and nothing else."""
    spoof = _make_service_info(
        address="DE:AD:BE:EF:00:01",
        rssi=-30,
        manufacturer_data={0x0042: b"\x03"},  # not Ratio's CIC
    )
    real = _make_service_info(
        address="79:75:75:A4:A0:45",
        rssi=-72,
        manufacturer_data={0x0BFF: b"\x03"},
    )
    coord = _make_coordinator(hass)

    with patch(
        "custom_components.ratio.ble.async_discovered_service_info",
        return_value=[spoof, real],
    ):
        device = coord._pick_best_device()

    assert device is real.device


@pytest.mark.asyncio
async def test_async_update_returns_cached_data(hass: HomeAssistant) -> None:
    """``_async_update`` is a no-op fallback — returns ``self.data`` if set."""
    from custom_components.ratio.ble import BleSnapshot

    coord = _make_coordinator(hass)
    snapshot = BleSnapshot(
        serial="SN001",
        voltage_phase_1=230.0,
        voltage_phase_2=None,
        voltage_phase_3=None,
        current_phase_1=None,
        current_phase_2=None,
        current_phase_3=None,
        protocol_version=2,
    )
    coord.data = snapshot
    result = await coord._async_update(_make_service_info())
    assert result is snapshot


@pytest.mark.asyncio
async def test_async_update_raises_when_no_data(hass: HomeAssistant) -> None:
    """No prior session data → UpdateFailed (signals stale session loop)."""
    coord = _make_coordinator(hass)
    coord.data = None
    with pytest.raises(UpdateFailed):
        await coord._async_update(_make_service_info())


@pytest.mark.asyncio
async def test_async_start_registers_local_name_callback(hass: HomeAssistant) -> None:
    """``async_start`` adds a stable local-name matcher in addition to parent."""
    coord = _make_coordinator(hass)
    parent_stop = MagicMock()
    local_unsub = MagicMock()

    captured: dict = {}

    def _capture_register(_hass, cb, matcher, mode):
        captured["matcher"] = matcher
        captured["cb"] = cb
        captured["mode"] = mode
        return local_unsub

    background_task = MagicMock()
    background_task.done.return_value = True

    def _swallow_coro(coro, _name):
        # Close the coroutine so we don't leak a "never awaited" warning.
        coro.close()
        return background_task

    with (
        patch.object(
            type(coord).__mro__[1],  # parent class
            "async_start",
            lambda self: parent_stop,
        ),
        patch(
            "custom_components.ratio.ble.async_register_callback",
            side_effect=_capture_register,
        ),
        patch(
            "custom_components.ratio.ble.async_discovered_service_info",
            return_value=[],
        ),
        patch.object(hass, "async_create_background_task", side_effect=_swallow_coro),
    ):
        stop = coord.async_start()

    # Local-name matcher uses RATIO_<serial> and connectable=True.
    matcher = captured["matcher"]
    assert dict(matcher).get("local_name") == "RATIO_SN001"
    assert dict(matcher).get("connectable") is True
    assert coord._local_name_unsub is local_unsub

    # Composite stop unwinds both subscriptions.
    stop()
    local_unsub.assert_called_once()
    parent_stop.assert_called_once()


@pytest.mark.asyncio
async def test_run_wifi_command_reuses_live_client(hass: HomeAssistant) -> None:
    """When the session loop owns a live client, ``run_wifi_command`` re-uses it."""
    coord = _make_coordinator(hass)
    live_client = MagicMock()
    live_client.is_connected = True
    coord._client = live_client

    seen: list[object] = []

    async def _fn(client) -> None:
        seen.append(client)

    with patch("custom_components.ratio.ble.BleClient") as ble_client_cls:
        await coord.run_wifi_command(_fn)

    ble_client_cls.assert_not_called()
    assert seen == [live_client]


@pytest.mark.asyncio
async def test_run_wifi_command_falls_back_to_one_shot(hass: HomeAssistant) -> None:
    """When no live client exists, a one-shot ``BleClient`` is constructed."""
    coord = _make_coordinator(hass)
    coord._client = None

    one_shot = MagicMock()
    one_shot.__aenter__ = AsyncMock(return_value=one_shot)
    one_shot.__aexit__ = AsyncMock(return_value=None)

    seen: list[object] = []

    async def _fn(client) -> None:
        seen.append(client)

    ble_device = MagicMock()
    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=ble_device,
        ),
        patch(
            "custom_components.ratio.ble.BleClient", return_value=one_shot
        ) as ble_client_cls,
    ):
        await coord.run_wifi_command(_fn)

    ble_client_cls.assert_called_once_with(ble_device)
    assert seen == [one_shot]


@pytest.mark.asyncio
async def test_run_wifi_command_raises_when_offline(hass: HomeAssistant) -> None:
    """No live client and no advert → ``RatioBleConnectionError``."""
    coord = _make_coordinator(hass)
    coord._client = None
    with (
        patch(
            "custom_components.ratio.ble.RatioBleCoordinator._pick_best_device",
            return_value=None,
        ),
        pytest.raises(RatioBleConnectionError),
    ):
        await coord.run_wifi_command(AsyncMock())
