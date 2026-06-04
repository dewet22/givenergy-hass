"""Tests for the battery out-of-spec binary sensor (issue #78)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.binary_sensor import (
    DEBOUNCE_MIN_POLLS,
    DEBOUNCE_SECONDS,
    GivEnergyBatteryOutOfSpecBinarySensor,
)
from custom_components.givenergy_local.const import DOMAIN

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _battery(serial="BT1", cells=None, temps=None):
    cells = cells if cells is not None else [3.30] * 16
    temps = temps if temps is not None else [22.0] * 4
    bat = SimpleNamespace(serial_number=serial)
    for i, volt in enumerate(cells, start=1):
        setattr(bat, f"v_cell_{i:02d}", volt)
    for (lo, hi), temp in zip(((1, 4), (5, 8), (9, 12), (13, 16)), temps):
        setattr(bat, f"t_cells_{lo:02d}_{hi:02d}", temp)
    return bat


def _entity(batteries):
    coordinator = MagicMock()
    coordinator.data.batteries = batteries
    coordinator.data.inverter_serial_number = "SA1234G123"
    coordinator.last_successful_refresh = None
    return GivEnergyBatteryOutOfSpecBinarySensor(coordinator), coordinator


def _poll(entity, coordinator, at: datetime, batteries=None) -> None:
    if batteries is not None:
        coordinator.data.batteries = batteries
    coordinator.last_successful_refresh = at
    entity._evaluate()


# ---------------------------------------------------------------------------
# Debounce state machine (unit-level)
# ---------------------------------------------------------------------------


def test_in_spec_never_trips():
    entity, coordinator = _entity([_battery()])
    for n in range(6):
        _poll(entity, coordinator, BASE + timedelta(seconds=n * DEBOUNCE_SECONDS))
    assert entity.is_on is False


def test_sustained_low_voltage_trips_after_time_and_polls():
    bad = [_battery(cells=[2.5] + [3.30] * 15)]
    entity, coordinator = _entity(bad)

    _poll(entity, coordinator, BASE)
    assert entity.is_on is False  # 1 poll, 0 s

    _poll(entity, coordinator, BASE + timedelta(seconds=150))
    assert entity.is_on is False  # 2 polls, 150 s — below both thresholds

    _poll(entity, coordinator, BASE + timedelta(seconds=DEBOUNCE_SECONDS))
    # 3 polls AND >= 300 s elapsed
    assert entity.is_on is True


def test_time_met_but_too_few_polls_does_not_trip():
    bad = [_battery(cells=[2.5] + [3.30] * 15)]
    entity, coordinator = _entity(bad)
    # Two polls far apart: duration satisfied, poll count is not.
    _poll(entity, coordinator, BASE)
    _poll(entity, coordinator, BASE + timedelta(seconds=DEBOUNCE_SECONDS * 2))
    assert DEBOUNCE_MIN_POLLS > 2
    assert entity.is_on is False


def test_transient_excursion_clears_and_resets():
    good = [_battery()]
    bad = [_battery(cells=[2.5] + [3.30] * 15)]
    entity, coordinator = _entity(good)

    # Out of spec for two polls (mimicking the ~2-min dongle garbage), then back.
    _poll(entity, coordinator, BASE, batteries=bad)
    _poll(entity, coordinator, BASE + timedelta(seconds=150), batteries=bad)
    _poll(entity, coordinator, BASE + timedelta(seconds=300), batteries=good)
    assert entity.is_on is False
    assert entity._offenders == {}

    # A fresh excursion starts its counter from scratch.
    _poll(entity, coordinator, BASE + timedelta(seconds=450), batteries=bad)
    assert entity._offenders["BT1:cell_01_voltage"].poll_count == 1


def test_unpopulated_cell_reading_zero_is_ignored():
    # Cell 16 unpopulated (~0 V) must not be read as an over-discharged cell.
    batteries = [_battery(cells=[3.30] * 15 + [0.0])]
    entity, coordinator = _entity(batteries)
    for n in range(5):
        _poll(entity, coordinator, BASE + timedelta(seconds=n * DEBOUNCE_SECONDS))
    assert entity.is_on is False


def test_sustained_overtemperature_trips():
    batteries = [_battery(temps=[60.0, 22.0, 22.0, 22.0])]
    entity, coordinator = _entity(batteries)
    for n in range(DEBOUNCE_MIN_POLLS):
        _poll(entity, coordinator, BASE + timedelta(seconds=n * 150))
    assert entity.is_on is True


def test_none_readings_are_skipped():
    bat = _battery()
    bat.v_cell_05 = None  # a dropped read on one cell
    bat.t_cells_05_08 = None
    entity, coordinator = _entity([bat])
    for n in range(5):
        _poll(entity, coordinator, BASE + timedelta(seconds=n * DEBOUNCE_SECONDS))
    assert entity.is_on is False


def test_attributes_enumerate_current_offenders():
    bad = [_battery(serial="BTX", cells=[2.5] + [3.30] * 15)]
    entity, coordinator = _entity(bad)
    _poll(entity, coordinator, BASE)
    _poll(entity, coordinator, BASE + timedelta(seconds=120))
    offenders = entity.extra_state_attributes["offenders"]
    assert len(offenders) == 1
    (offender,) = offenders
    assert offender["battery"] == "BTX"
    assert offender["metric"] == "cell_01_voltage"
    assert offender["polls_out_of_spec"] == 2
    assert offender["seconds_out_of_spec"] == 120


# ---------------------------------------------------------------------------
# Creation gating (integration-level)
# ---------------------------------------------------------------------------


def _entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, unique_id)


async def test_entity_created_and_off_for_in_spec_plant(hass, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_out_of_spec")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "off"


async def test_not_created_for_battery_less_plant(hass, mock_client, mock_plant, mock_config_entry):
    mock_plant.batteries = []
    mock_plant.number_batteries = 0
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _entity_id(hass, "SA1234G123_battery_out_of_spec") is None
