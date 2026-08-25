"""Contract tests against the REAL ``aioratio`` client.

Every other test module replaces the cloud setters with ``AsyncMock`` (see
``conftest._make_client_instance``), so they pin the DTO the integration
builds and its ``to_dict()`` output — but nothing downstream of it. The
client's ``_coerce_body()``, the ``{transactionId, <kind>Settings}`` envelope,
the ``?id=`` query parameter and the ``set_charge_schedule()`` type guard were
all unexercised, which is why bumping to a BREAKING ``aioratio`` 0.12.0 turned
zero tests red.

These tests wire a real :class:`aioratio.RatioClient` to a recording transport
— the same approach as ``aioratio``'s own ``tests/test_client.py`` — and
assert the exact JSON that would go on the wire.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioratio import RatioClient
from aioratio.models import ChargeSchedule, ScheduleSlot
from aioratio.token_store import MemoryTokenStore, TokenBundle
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ratio.const import DOMAIN
from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.number import RatioMaximumChargingCurrentNumber
from custom_components.ratio.select import RatioCpmsSelect
from custom_components.ratio.switch import RatioOcppEnabledSwitch

SERIAL = "SN-CONTRACT"
USER_ID = "user-abc"

_TXN_RE = re.compile(r"[0-9a-f]{16}")

_EMPTY_WEEK: dict[str, list[dict[str, int]]] = {
    "monday": [],
    "tuesday": [],
    "wednesday": [],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}


class RecordingTransport:
    """Stand-in for ``aioratio._transport._CloudTransport``.

    Mirrors ``aioratio``'s own ``FakeTransport``: it captures the fully
    prepared request instead of sending it.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "params": params, "json": json}
        )
        return None

    @property
    def body(self) -> dict[str, Any]:
        """Return the single recorded request body."""
        assert len(self.calls) == 1, f"expected exactly one call, got {self.calls}"
        return cast(dict[str, Any], self.calls[0]["json"])


def _id_token(sub: str = USER_ID) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


@pytest.fixture
async def real_client() -> AsyncGenerator[tuple[RatioClient, RecordingTransport], None]:
    """A real ``RatioClient`` whose transport records instead of sending.

    Only the transport is faked. ``_coerce_body()``, ``_put_settings()``, the
    ``set_charge_schedule()`` guard and ``user_id()`` are the shipped code.
    The session placeholder is never touched: the recording transport
    replaces the only component that would use it, and a non-expired bundle
    in the store keeps ``get_access_token()`` offline. Creating a real
    ``aiohttp.ClientSession`` here would leave pycares' shutdown thread
    behind and trip HA's ``verify_cleanup``.
    """
    store = MemoryTokenStore()
    await store.save(
        TokenBundle(
            access_token="ACCESS",
            id_token=_id_token(),
            refresh_token="REFRESH",
            expires_at=time.time() + 3600,
        )
    )
    client = RatioClient(token_store=store, session=cast(Any, MagicMock()))
    transport = RecordingTransport()
    client._transport = cast(Any, transport)
    yield client, transport


def _assert_settings_envelope(
    call: dict[str, Any], *, kind: str, inner: dict[str, Any]
) -> None:
    """Assert the full PUT shape produced by ``RatioClient._put_settings``."""
    assert call["method"] == "PUT"
    assert call["path"] == f"/users/{USER_ID}/chargers/{SERIAL}/settings"
    assert call["params"] == {"id": kind}
    body = call["json"]
    assert set(body) == {"transactionId", f"{kind}Settings"}
    assert _TXN_RE.fullmatch(body["transactionId"])
    assert body[f"{kind}Settings"] == inner
    # The transport hands this straight to aiohttp's JSON encoder.
    json.dumps(body)


def _passthrough_coordinator(data: RatioData) -> MagicMock:
    """A coordinator stub whose ``request_command`` really awaits the call."""
    coord = MagicMock()
    coord.data = data

    async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(*args, **kwargs)

    coord.request_command = AsyncMock(side_effect=_run)
    return coord


# ---------------------------------------------------------------------------
# Number write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_number_write_reaches_the_transport_as_a_json_int(
    real_client: tuple[RatioClient, RecordingTransport],
) -> None:
    """``SetUserSettings`` types the field ``Int?``; a float is a wire violation."""
    from aioratio.models import UserSettings
    from aioratio.models.settings import UpperLowerLimitSetting

    client, transport = real_client
    coord = _passthrough_coordinator(
        RatioData(
            user_settings={
                SERIAL: UserSettings(
                    maximum_charging_current=UpperLowerLimitSetting(
                        value=16.0, lower=6.0, upper=32.0
                    )
                )
            }
        )
    )

    entity = RatioMaximumChargingCurrentNumber(coord, client, SERIAL)
    await entity.async_set_native_value(20.0)

    _assert_settings_envelope(
        transport.calls[0], kind="user", inner={"maximumChargingCurrent": 20}
    )
    value = transport.body["userSettings"]["maximumChargingCurrent"]
    assert type(value) is int
    assert json.loads(json.dumps(transport.body))["userSettings"] == {
        "maximumChargingCurrent": 20
    }


# ---------------------------------------------------------------------------
# OCPP write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ocpp_switch_write_reaches_the_transport(
    real_client: tuple[RatioClient, RecordingTransport],
) -> None:
    """Sparse ``SetInstallerOcppSettings``: only ``enabled``, under ``?id=installerOcpp``."""
    from aioratio.models import InstallerOcppSettings

    client, transport = real_client
    coord = _passthrough_coordinator(
        RatioData(ocpp_settings={SERIAL: InstallerOcppSettings(enabled=False)})
    )

    entity = RatioOcppEnabledSwitch(coord, client, SERIAL)
    await entity.async_turn_on()

    _assert_settings_envelope(
        transport.calls[0], kind="installerOcpp", inner={"enabled": True}
    )


@pytest.mark.asyncio
async def test_cpms_select_write_reaches_the_transport(
    real_client: tuple[RatioClient, RecordingTransport],
) -> None:
    """Sparse ``SetInstallerOcppSettings``: only ``cpms``, both keys present.

    ``ConfiguredCpms$$serializer.java``:40-47 declares ``centralSystem`` and
    ``url`` required and non-nullable, so the body must carry both.
    """
    from aioratio.models import CpmsConfig, InstallerOcppSettings

    client, transport = real_client
    coord = _passthrough_coordinator(
        RatioData(
            ocpp_settings={SERIAL: InstallerOcppSettings()},
            cpms_options={
                SERIAL: [
                    CpmsConfig(central_system="Op A", url="ws://a.com"),
                    CpmsConfig(url="ws://orphan.com"),
                ]
            },
        )
    )

    entity = RatioCpmsSelect(coord, client, SERIAL)
    # The unwritable, URL-only entry is never offered.
    assert entity.options == ["Op A"]
    await entity.async_select_option("Op A")

    _assert_settings_envelope(
        transport.calls[0],
        kind="installerOcpp",
        inner={"cpms": {"centralSystem": "Op A", "url": "ws://a.com"}},
    )


# ---------------------------------------------------------------------------
# Charge schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_schedule_service_reaches_the_transport(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    setup_integration: MockConfigEntry,
    real_client: tuple[RatioClient, RecordingTransport],
) -> None:
    """The whole ``ratio.set_schedule`` path down to the JSON body.

    ``WeekPlanViewModel.java:99`` sends exactly ``enabled`` + ``scheduleType``
    + ``weekSchedule``, and ``WeekScheduleSetting$$serializer.java`` requires
    all seven day keys.
    """
    entry = setup_integration
    client, transport = real_client

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
    )

    mock_client = entry.runtime_data.client
    entry.runtime_data.client = client
    try:
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device.id,
                "slots": [
                    {"start": "22:00", "end": "06:00", "days": ["monday", "tuesday"]}
                ],
            },
            blocking=True,
        )
    finally:
        entry.runtime_data.client = mock_client

    slot = {
        "beginTimeHour": 22,
        "beginTimeMinute": 0,
        "endTimeHour": 6,
        "endTimeMinute": 0,
    }
    _assert_settings_envelope(
        transport.calls[0],
        kind="chargeSchedule",
        inner={
            "enabled": True,
            "scheduleType": "WeekSchedule",
            "weekSchedule": {**_EMPTY_WEEK, "monday": [slot], "tuesday": [slot]},
        },
    )
    week = transport.body["chargeScheduleSettings"]["weekSchedule"]
    assert set(week) == set(_EMPTY_WEEK)


@pytest.mark.asyncio
async def test_set_charge_schedule_rejects_the_get_model(
    real_client: tuple[RatioClient, RecordingTransport],
) -> None:
    """aioratio 0.12.0 refuses ``ChargeSchedule``; 0.11 silently accepted it.

    The GET model's ``bool`` fields cannot express "leave unchanged", so
    writing one back reasserts ``enabled`` and the whole stored week plan.
    """
    client, transport = real_client

    schedule = ChargeSchedule(
        enabled=True,
        schedule_type="WeekSchedule",
        slots=[ScheduleSlot(start="22:00", end="06:00", days=["monday"])],
    )
    with pytest.raises(TypeError, match="ChargeScheduleUpdate"):
        await client.set_charge_schedule(SERIAL, cast(Any, schedule))

    assert transport.calls == []
