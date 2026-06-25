"""Tests for the inverter system-clock drift repair (#219 follow-up)."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.givenergy_local import _check_system_time_drift
from custom_components.givenergy_local.const import (
    CONF_BATTERY_DATA_ONLY,
    CONF_WARN_CLOCK_DRIFT,
    DOMAIN,
    system_time_drift,
)
from custom_components.givenergy_local.repairs import (
    SystemTimeDriftRepairFlow,
    async_create_fix_flow,
)


def _drifted(minutes: int):
    """A naive local wall-clock `minutes` ahead of now (the inverter clock shape)."""
    return (dt_util.now() + timedelta(minutes=minutes)).replace(tzinfo=None)


def _entry(entry_id: str = "entry-1", **options):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = dict(options)
    return entry


def _coordinator(system_time):
    coord = MagicMock()
    coord.data.inverter.system_time = system_time
    return coord


def _issue(hass, entry_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"system_time_drift_{entry_id}")


# --- pure helper -------------------------------------------------------------


def test_system_time_drift_helper():
    now = dt_util.now()
    assert system_time_drift(None, now) is None
    assert system_time_drift(now.replace(tzinfo=None), now) < timedelta(seconds=1)
    assert system_time_drift(_drifted(15), now) >= timedelta(minutes=14)


# --- _check_system_time_drift: raise / clear / guards ------------------------


async def test_drift_beyond_threshold_raises_issue(hass):
    entry = _entry()
    _check_system_time_drift(hass, entry, _coordinator(_drifted(15)))
    assert _issue(hass, entry.entry_id) is not None


async def test_drift_within_threshold_no_issue(hass):
    entry = _entry()
    _check_system_time_drift(hass, entry, _coordinator(_drifted(2)))
    assert _issue(hass, entry.entry_id) is None


async def test_drift_issue_clears_when_back_in_window(hass):
    entry = _entry()
    coord = _coordinator(_drifted(15))
    _check_system_time_drift(hass, entry, coord)
    assert _issue(hass, entry.entry_id) is not None
    coord.data.inverter.system_time = dt_util.now().replace(tzinfo=None)
    _check_system_time_drift(hass, entry, coord)
    assert _issue(hass, entry.entry_id) is None


async def test_none_clock_does_not_raise(hass):
    entry = _entry()
    _check_system_time_drift(hass, entry, _coordinator(None))
    assert _issue(hass, entry.entry_id) is None


async def test_battery_data_only_entry_skipped(hass):
    entry = _entry(**{CONF_BATTERY_DATA_ONLY: True})
    _check_system_time_drift(hass, entry, _coordinator(_drifted(15)))
    assert _issue(hass, entry.entry_id) is None


async def test_toggle_off_does_not_raise(hass):
    entry = _entry(**{CONF_WARN_CLOCK_DRIFT: False})
    _check_system_time_drift(hass, entry, _coordinator(_drifted(15)))
    assert _issue(hass, entry.entry_id) is None


async def test_toggle_off_clears_standing_issue(hass):
    entry = _entry()
    coord = _coordinator(_drifted(15))
    _check_system_time_drift(hass, entry, coord)
    assert _issue(hass, entry.entry_id) is not None
    entry.options = {CONF_WARN_CLOCK_DRIFT: False}
    _check_system_time_drift(hass, entry, coord)
    assert _issue(hass, entry.entry_id) is None


# --- end-to-end: listener wired at setup -------------------------------------


async def test_issue_raised_at_setup_when_drifted(
    hass, mock_client, mock_inverter, mock_config_entry
):
    mock_inverter.system_time = _drifted(15)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, mock_config_entry.entry_id) is not None


async def test_no_issue_at_setup_when_in_window(
    hass, mock_client, mock_inverter, mock_config_entry
):
    mock_inverter.system_time = dt_util.now().replace(tzinfo=None)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, mock_config_entry.entry_id) is None


# --- fix flow ----------------------------------------------------------------


async def test_create_fix_flow_dispatches_to_drift_flow(hass):
    flow = await async_create_fix_flow(hass, "system_time_drift_xyz", {"entry_id": "xyz"})
    assert isinstance(flow, SystemTimeDriftRepairFlow)


async def test_fix_flow_form_surfaces_drift(hass):
    flow = SystemTimeDriftRepairFlow(
        {"entry_id": "e", "drift_minutes": "15", "system_time": "a", "ha_time": "b"}
    )
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == FlowResultType.FORM
    assert result["description_placeholders"]["drift_minutes"] == "15"


async def test_fix_flow_syncs_clock_to_now(hass, mock_client):
    coord = MagicMock()
    coord._client = mock_client  # connected=True, one_shot_command AsyncMock
    coord.async_request_refresh = mock_client.refresh  # any AsyncMock
    hass.data.setdefault(DOMAIN, {})["entry-1"] = coord

    flow = SystemTimeDriftRepairFlow({"entry_id": "entry-1"})
    flow.hass = hass
    result = await flow.async_step_init({})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.one_shot_command.assert_awaited_once()
    # The write is set_system_date_time(now) → six RTC registers, year as year-2000.
    year, month, day, hour, minute, second = mock_client.one_shot_command.call_args[0][0]
    now_local = dt_util.now()
    assert year.value == now_local.year - 2000
    assert month.value == now_local.month
    assert day.value == now_local.day


async def test_fix_flow_raises_when_disconnected(hass, mock_client):
    mock_client.connected = False
    coord = MagicMock()
    coord._client = mock_client
    hass.data.setdefault(DOMAIN, {})["entry-1"] = coord
    flow = SystemTimeDriftRepairFlow({"entry_id": "entry-1"})
    flow.hass = hass
    with pytest.raises(HomeAssistantError):
        await flow.async_step_init({})
