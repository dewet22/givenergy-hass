"""Money sensors: tariff-priced energy accumulators (mission dashboard, Phase 1).

Four sensors per inverter price the day's energy flows against user-named
tariff rate entities: grid_import_cost_today, grid_export_earnings_today,
net_energy_cost_today and counterfactual_cost_today. They are created only
when the config entry's options name the tariff entities, accumulate
incrementally per coordinator tick, reset with the underlying ``_today``
sources at midnight, and go unavailable (rather than pricing at zero) while a
tariff entity is unavailable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.givenergy_local.const import DOMAIN

IMPORT_RATE = "sensor.tariff_import"
EXPORT_RATE = "sensor.tariff_export"

MONEY_KEYS = (
    "grid_import_cost_today",
    "grid_export_earnings_today",
    "net_energy_cost_today",
    "counterfactual_cost_today",
)


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, f"No entity found for unique_id={unique_id!r}"
    return entity_id


def _state(hass, key: str):
    return hass.states.get(_entity_id(hass, f"SA1234G123_{key}"))


def _set_rate(hass, entity_id: str, value, unit: str = "GBP/kWh") -> None:
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


@pytest.fixture
def money_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 8899,
            "scan_interval": 30,
            "passive": False,
        },
        options={
            "tariff_import_entity": IMPORT_RATE,
            "tariff_export_entity": EXPORT_RATE,
        },
        unique_id="SA1234G123",
    )


@pytest.fixture
async def setup_money(hass, mock_client, money_config_entry):
    """Set up the integration with tariff options and live rate entities."""
    _set_rate(hass, IMPORT_RATE, 0.30)
    _set_rate(hass, EXPORT_RATE, 0.15)
    money_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(money_config_entry.entry_id)
    await hass.async_block_till_done()
    return money_config_entry


async def _tick(hass, entry, mock_inverter, **changes) -> None:
    """Simulate one coordinator refresh with updated inverter readings."""
    for attr, value in changes.items():
        setattr(mock_inverter, attr, value)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()


# --- creation gating ---------------------------------------------------------


async def test_money_sensors_absent_without_options(hass, setup_integration):
    registry = er.async_get(hass)
    for key in MONEY_KEYS:
        assert registry.async_get_entity_id("sensor", DOMAIN, f"SA1234G123_{key}") is None


async def test_money_sensors_created_with_options(hass, setup_money):
    for key in MONEY_KEYS:
        state = _state(hass, key)
        assert state is not None
        assert float(state.state) == 0.0
        assert state.attributes["device_class"] == "monetary"
        # Currency derived from the rate entity's unit (GBP/kWh -> GBP).
        assert state.attributes["unit_of_measurement"] == "GBP"
        assert state.attributes["state_class"] == "total"


async def test_money_sensors_start_at_zero_mid_day(hass, setup_money):
    """Energy accrued before the sensors existed is never retro-priced."""
    # The fixture inverter starts with e_grid_in_day=5.3 already on the clock.
    assert float(_state(hass, "grid_import_cost_today").state) == 0.0


# --- accumulation ------------------------------------------------------------


async def test_import_cost_accumulates_per_tick(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 kWh @ 0.30
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.30)
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.8)  # +0.5 kWh @ 0.30
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.45)


async def test_rate_change_prices_each_delta_at_rate_in_force(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 @ 0.30
    _set_rate(hass, IMPORT_RATE, 0.10)
    await hass.async_block_till_done()
    await _tick(hass, entry, mock_inverter, e_grid_in_day=8.3)  # +2.0 @ 0.10
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.50)


async def test_pence_rates_normalised_to_currency(hass, setup_money, mock_inverter):
    entry = setup_money
    _set_rate(hass, IMPORT_RATE, 30, unit="p/kWh")
    await hass.async_block_till_done()
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 kWh @ 30p
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.30)
    state = _state(hass, "grid_import_cost_today")
    assert state.attributes["unit_of_measurement"] == "GBP"


async def test_export_earnings_accumulate(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_out_day=5.1)  # +3.0 kWh @ 0.15
    assert float(_state(hass, "grid_export_earnings_today").state) == pytest.approx(0.45)


async def test_net_cost_is_import_minus_export(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=7.3, e_grid_out_day=3.1)
    # +2.0 kWh import @ 0.30 = 0.60; +1.0 kWh export @ 0.15 = 0.15
    assert float(_state(hass, "net_energy_cost_today").state) == pytest.approx(0.45)


async def test_net_cost_can_go_negative(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_out_day=8.1)  # +6.0 kWh export @ 0.15
    assert float(_state(hass, "net_energy_cost_today").state) == pytest.approx(-0.90)


async def test_counterfactual_prices_consumption_at_import_rate(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_consumption_today=23.4)  # +2.0 kWh @ 0.30
    state = _state(hass, "counterfactual_cost_today")
    assert float(state.state) == pytest.approx(0.60)


async def test_savings_today_attribute_is_counterfactual_minus_net(
    hass, setup_money, mock_inverter
):
    entry = setup_money
    await _tick(
        hass,
        entry,
        mock_inverter,
        e_grid_in_day=6.3,  # +1.0 @ 0.30 -> import 0.30
        e_grid_out_day=3.1,  # +1.0 @ 0.15 -> export 0.15; net 0.15
        e_consumption_today=25.4,  # +4.0 @ 0.30 -> counterfactual 1.20
    )
    state = _state(hass, "counterfactual_cost_today")
    assert state.attributes["savings_today"] == pytest.approx(1.05)


# --- midnight reset ----------------------------------------------------------


async def test_midnight_reset_of_source_resets_accumulator(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 @ 0.30
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.30)
    # Source sensor resets at midnight: 6.3 -> 0.2. The new day's total is
    # priced from zero at the current rate.
    await _tick(hass, entry, mock_inverter, e_grid_in_day=0.2)
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.06)


async def test_last_reset_is_local_midnight(hass, setup_money):
    state = _state(hass, "grid_import_cost_today")
    last_reset = dt_util.parse_datetime(state.attributes["last_reset"])
    assert last_reset == dt_util.start_of_local_day()


# --- tariff outage -----------------------------------------------------------


async def test_unavailable_rate_makes_sensor_unavailable(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 @ 0.30
    hass.states.async_set(IMPORT_RATE, "unavailable")
    await hass.async_block_till_done()
    await _tick(hass, entry, mock_inverter, e_grid_in_day=7.3)
    assert _state(hass, "grid_import_cost_today").state == "unavailable"


async def test_outage_gap_is_not_priced_on_recovery(hass, setup_money, mock_inverter):
    entry = setup_money
    await _tick(hass, entry, mock_inverter, e_grid_in_day=6.3)  # +1.0 @ 0.30
    hass.states.async_set(IMPORT_RATE, "unavailable")
    await hass.async_block_till_done()
    # 2.0 kWh arrives while the rate is unknown: it must not be priced, at
    # either the old or the recovery rate.
    await _tick(hass, entry, mock_inverter, e_grid_in_day=8.3)
    _set_rate(hass, IMPORT_RATE, 0.50)
    await hass.async_block_till_done()
    await _tick(hass, entry, mock_inverter, e_grid_in_day=9.3)  # +1.0 @ 0.50
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(0.80)


async def test_export_sensor_unaffected_by_import_outage(hass, setup_money, mock_inverter):
    entry = setup_money
    hass.states.async_set(IMPORT_RATE, "unavailable")
    await hass.async_block_till_done()
    await _tick(hass, entry, mock_inverter, e_grid_out_day=3.1)  # +1.0 @ 0.15
    assert float(_state(hass, "grid_export_earnings_today").state) == pytest.approx(0.15)


# --- restart restore ---------------------------------------------------------


async def test_restore_resumes_same_day_total(hass, mock_client, money_config_entry):
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.givenergy_inverter_sa1234g123_grid_import_cost_today",
                "1.23",
            ),
        ),
    )
    _set_rate(hass, IMPORT_RATE, 0.30)
    _set_rate(hass, EXPORT_RATE, 0.15)
    money_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(money_config_entry.entry_id)
    await hass.async_block_till_done()
    assert float(_state(hass, "grid_import_cost_today").state) == pytest.approx(1.23)


async def test_restore_ignores_previous_day_total(hass, mock_client, money_config_entry):
    yesterday = dt_util.utcnow() - timedelta(days=1)
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.givenergy_inverter_sa1234g123_grid_import_cost_today",
                "1.23",
                last_updated=yesterday,
            ),
        ),
    )
    _set_rate(hass, IMPORT_RATE, 0.30)
    _set_rate(hass, EXPORT_RATE, 0.15)
    money_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(money_config_entry.entry_id)
    await hass.async_block_till_done()
    assert float(_state(hass, "grid_import_cost_today").state) == 0.0


async def test_restore_feeds_net_cost(hass, mock_client, money_config_entry):
    """Restored per-sensor totals land in the shared tracker, so net sees them."""
    mock_restore_cache(
        hass,
        (
            State(
                "sensor.givenergy_inverter_sa1234g123_grid_import_cost_today",
                "1.20",
            ),
            State(
                "sensor.givenergy_inverter_sa1234g123_grid_export_earnings_today",
                "0.50",
            ),
        ),
    )
    _set_rate(hass, IMPORT_RATE, 0.30)
    _set_rate(hass, EXPORT_RATE, 0.15)
    money_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(money_config_entry.entry_id)
    await hass.async_block_till_done()
    assert float(_state(hass, "net_energy_cost_today").state) == pytest.approx(0.70)


# --- options wiring ----------------------------------------------------------


async def test_setting_options_creates_sensors_via_reload(hass, mock_client, mock_config_entry):
    """Adding tariff options to a live entry reloads it and creates the sensors."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_net_energy_cost_today") is None
    )

    _set_rate(hass, IMPORT_RATE, 0.30)
    _set_rate(hass, EXPORT_RATE, 0.15)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            "tariff_import_entity": IMPORT_RATE,
            "tariff_export_entity": EXPORT_RATE,
        },
    )
    await hass.async_block_till_done()
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_net_energy_cost_today")
        is not None
    )
