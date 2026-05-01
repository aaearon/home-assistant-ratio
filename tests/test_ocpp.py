"""Tests for OCPP switch, CPMS select, and charge point identifier text entities."""
from __future__ import annotations

import pytest
from aioratio.models import ChargerOverview, CpmsConfig, InstallerOcppSettings, OcppFieldStatus
from aioratio.models.diagnostics import ChargerDiagnostics

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.ratio.coordinator import RatioData
from custom_components.ratio.select import RatioCpmsSelect
from custom_components.ratio.switch import RatioOcppEnabledSwitch
from custom_components.ratio.text import RatioChargePointIdentifierText


SERIAL = "SN001"


def _make_overview(serial: str = SERIAL) -> ChargerOverview:
    return ChargerOverview.from_dict({"serialNumber": serial})


def _make_ocpp(
    enabled: bool = True,
    cpms_url: str = "ws://op.com",
    cpid: str = "CP-001",
    enabled_allowed: bool = True,
    cpms_allowed: bool = True,
    cpid_allowed: bool = True,
    cpid_max_length: int = 48,
) -> InstallerOcppSettings:
    return InstallerOcppSettings(
        enabled=enabled,
        cpms=CpmsConfig(central_system="Op", url=cpms_url),
        charge_point_identifier=cpid,
        enabled_status=OcppFieldStatus(is_change_allowed=enabled_allowed),
        cpms_status=OcppFieldStatus(is_change_allowed=cpms_allowed),
        charge_point_identifier_status=OcppFieldStatus(is_change_allowed=cpid_allowed),
        charge_point_identifier_max_length=cpid_max_length,
    )


def _coord(ocpp: InstallerOcppSettings, cpms_opts: list[CpmsConfig] | None = None) -> MagicMock:
    coord = MagicMock()
    coord.data = RatioData(
        chargers={SERIAL: _make_overview()},
        ocpp_settings={SERIAL: ocpp},
        cpms_options={SERIAL: cpms_opts or []},
    )
    coord.last_update_success = True
    coord.request_command = AsyncMock()
    return coord


# ---------------------------------------------------------------------------
# OCPP switch
# ---------------------------------------------------------------------------


def test_ocpp_switch_is_on_when_enabled() -> None:
    coord = _coord(_make_ocpp(enabled=True))
    sw = RatioOcppEnabledSwitch(coord, MagicMock(), SERIAL)
    assert sw.is_on is True


def test_ocpp_switch_is_off_when_disabled() -> None:
    coord = _coord(_make_ocpp(enabled=False))
    sw = RatioOcppEnabledSwitch(coord, MagicMock(), SERIAL)
    assert sw.is_on is False


def test_ocpp_switch_unavailable_when_not_allowed() -> None:
    coord = _coord(_make_ocpp(enabled_allowed=False))
    sw = RatioOcppEnabledSwitch(coord, MagicMock(), SERIAL)
    assert sw.available is False


def test_ocpp_switch_available_when_allowed() -> None:
    coord = _coord(_make_ocpp(enabled_allowed=True))
    sw = RatioOcppEnabledSwitch(coord, MagicMock(), SERIAL)
    assert sw.available is True


def test_ocpp_switch_extra_attrs_when_not_allowed() -> None:
    ocpp = InstallerOcppSettings(
        enabled_status=OcppFieldStatus(
            is_change_allowed=False,
            change_not_allowed_reason="MANAGED_BY_OPERATOR",
        )
    )
    coord = _coord(ocpp)
    sw = RatioOcppEnabledSwitch(coord, MagicMock(), SERIAL)
    attrs = sw.extra_state_attributes
    assert attrs is not None
    assert attrs["change_not_allowed_reason"] == "MANAGED_BY_OPERATOR"


@pytest.mark.asyncio
async def test_ocpp_switch_turn_on_calls_set_ocpp() -> None:
    coord = _coord(_make_ocpp(enabled=False))
    client = MagicMock()
    sw = RatioOcppEnabledSwitch(coord, client, SERIAL)
    await sw.async_turn_on()
    coord.request_command.assert_awaited_once()
    _, settings = coord.request_command.call_args[0][1:]
    assert settings.enabled is True


@pytest.mark.asyncio
async def test_ocpp_switch_turn_off_calls_set_ocpp() -> None:
    coord = _coord(_make_ocpp(enabled=True))
    client = MagicMock()
    sw = RatioOcppEnabledSwitch(coord, client, SERIAL)
    await sw.async_turn_off()
    coord.request_command.assert_awaited_once()
    _, settings = coord.request_command.call_args[0][1:]
    assert settings.enabled is False


# ---------------------------------------------------------------------------
# CPMS select
# ---------------------------------------------------------------------------


def _cpms_select(coord: MagicMock) -> RatioCpmsSelect:
    return RatioCpmsSelect(coord, MagicMock(), SERIAL)


def test_cpms_select_options_from_list() -> None:
    opts = [
        CpmsConfig(central_system="Op A", url="ws://a.com"),
        CpmsConfig(central_system="Op B", url="ws://b.com"),
    ]
    coord = _coord(_make_ocpp(cpms_url="ws://a.com"), cpms_opts=opts)
    sel = _cpms_select(coord)
    assert "Op A" in sel.options
    assert "Op B" in sel.options


def test_cpms_select_current_option_matches_url() -> None:
    opts = [
        CpmsConfig(central_system="Op A", url="ws://a.com"),
        CpmsConfig(central_system="Op B", url="ws://b.com"),
    ]
    coord = _coord(_make_ocpp(cpms_url="ws://b.com"), cpms_opts=opts)
    sel = _cpms_select(coord)
    assert sel.current_option == "Op B"


def test_cpms_select_unavailable_when_not_allowed() -> None:
    coord = _coord(_make_ocpp(cpms_allowed=False))
    sel = _cpms_select(coord)
    assert sel.available is False


def test_cpms_select_falls_back_to_current_cpms_if_no_list() -> None:
    """With no CPMS options list, the select falls back to the current CPMS."""
    coord = _coord(_make_ocpp(cpms_url="ws://op.com"))  # no cpms_opts
    sel = _cpms_select(coord)
    assert "Op" in sel.options
    assert sel.current_option is not None


@pytest.mark.asyncio
async def test_cpms_select_calls_set_ocpp_with_selected_url() -> None:
    opts = [
        CpmsConfig(central_system="Op A", url="ws://a.com"),
        CpmsConfig(central_system="Op B", url="ws://b.com"),
    ]
    coord = _coord(_make_ocpp(), cpms_opts=opts)
    sel = _cpms_select(coord)
    await sel.async_select_option("Op A")
    coord.request_command.assert_awaited_once()
    _, settings = coord.request_command.call_args[0][1:]
    assert settings.cpms is not None
    assert settings.cpms.url == "ws://a.com"


# ---------------------------------------------------------------------------
# Charge point identifier text
# ---------------------------------------------------------------------------


def _cpid_text(coord: MagicMock) -> RatioChargePointIdentifierText:
    return RatioChargePointIdentifierText(coord, MagicMock(), SERIAL)


def test_cpid_text_native_value() -> None:
    coord = _coord(_make_ocpp(cpid="CP-TEST"))
    assert _cpid_text(coord).native_value == "CP-TEST"


def test_cpid_text_unavailable_when_not_allowed() -> None:
    coord = _coord(_make_ocpp(cpid_allowed=False))
    text = _cpid_text(coord)
    assert text.available is False


def test_cpid_text_available_when_allowed() -> None:
    coord = _coord(_make_ocpp(cpid_allowed=True))
    text = _cpid_text(coord)
    assert text.available is True


def test_cpid_text_native_max_from_settings() -> None:
    coord = _coord(_make_ocpp(cpid_max_length=64))
    assert _cpid_text(coord).native_max == 64


def test_cpid_text_native_max_defaults_to_255_when_unset() -> None:
    coord = _coord(InstallerOcppSettings())
    assert _cpid_text(coord).native_max == 255


def test_cpid_text_extra_attrs_when_not_allowed() -> None:
    ocpp = InstallerOcppSettings(
        charge_point_identifier_status=OcppFieldStatus(
            is_change_allowed=False,
            change_not_allowed_reason="MANAGED_BY_OPERATOR",
        )
    )
    coord = _coord(ocpp)
    attrs = _cpid_text(coord).extra_state_attributes
    assert attrs is not None
    assert attrs["change_not_allowed_reason"] == "MANAGED_BY_OPERATOR"


@pytest.mark.asyncio
async def test_cpid_text_set_value_calls_set_ocpp() -> None:
    coord = _coord(_make_ocpp(cpid="OLD"))
    text = _cpid_text(coord)
    await text.async_set_value("NEW-CP")
    coord.request_command.assert_awaited_once()
    _, settings = coord.request_command.call_args[0][1:]
    assert settings.charge_point_identifier == "NEW-CP"
