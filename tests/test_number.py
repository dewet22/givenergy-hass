"""Tests for the GivEnergy Local number platform."""

import pytest
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
    assert entity_id is not None, f"No number entity for unique_id={unique_id!r}"
    return entity_id


def _maybe_entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("number", DOMAIN, unique_id)


async def test_charge_target_soc_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_charge_target_soc"))
    assert float(state.state) == 100.0


async def test_battery_soc_reserve_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_soc_reserve"))
    assert float(state.state) == 4.0


async def test_set_charge_target_soc_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_charge_target_soc")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 80}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


async def test_set_battery_discharge_limit_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_discharge_limit")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 25}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


async def test_active_power_rate_present(hass, setup_integration):
    # Entity is wired (the write path is covered below); this fixture's register
    # set doesn't populate active_power_rate, so the decoded value may be unknown.
    assert hass.states.get(_entity_id(hass, "SA1234G123_active_power_rate")) is not None


async def test_set_active_power_rate_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_active_power_rate")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 90}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


# ---------------------------------------------------------------------------
# AC-config-block limits — created for AC-coupled inverters and single-phase AIO
# ---------------------------------------------------------------------------


@pytest.fixture
async def ac_coupled_setup(hass, mock_client, mock_plant, mock_inverter, mock_config_entry):
    """Set up the integration with a single-phase AC-coupled plant."""
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.AC,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.battery_charge_limit_ac = 50
    mock_inverter.battery_discharge_limit_ac = 60
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_ac_limits_absent_on_hybrid_plant(hass, setup_integration):
    """The default fixture has device_type=Model.HYBRID — AC limits must not be created."""
    assert _maybe_entity_id(hass, "SA1234G123_battery_charge_limit_ac") is None
    assert _maybe_entity_id(hass, "SA1234G123_battery_discharge_limit_ac") is None


async def test_ac_limits_present_on_ac_coupled_plant(hass, ac_coupled_setup):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_charge_limit_ac"))
    assert state is not None
    assert float(state.state) == 50.0
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100


async def test_ac_limits_absent_when_register_unreadable(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """#207: on an AC-coupled plant whose firmware doesn't carry HR313/314 (they read
    None), the AC-limit controls aren't created — no broken slider."""
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.AC,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.battery_charge_limit_ac = None
    mock_inverter.battery_discharge_limit_ac = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _maybe_entity_id(hass, "SA1234G123_battery_charge_limit_ac") is None
    assert _maybe_entity_id(hass, "SA1234G123_battery_discharge_limit_ac") is None


def test_ac_limit_kept_on_partial_seed(mock_inverter):
    """#208: on a partial seed poll (clean=False) a None read may be a transient bank
    failure, so the control is kept rather than gated."""
    from custom_components.givenergy_local.number import (
        AC_COUPLED_NUMBER_DESCRIPTIONS,
        _include_number,
    )

    desc = next(d for d in AC_COUPLED_NUMBER_DESCRIPTIONS if d.key == "battery_charge_limit_ac")
    mock_inverter.battery_charge_limit_ac = None
    assert _include_number(desc, mock_inverter, clean=True) is False  # clean + None → gated
    assert _include_number(desc, mock_inverter, clean=False) is True  # partial → kept


async def test_set_ac_charge_limit_sends_command(hass, mock_client, ac_coupled_setup):
    entity_id = _entity_id(hass, "SA1234G123_battery_charge_limit_ac")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 40}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


@pytest.mark.parametrize("value", [0, 100])
async def test_dc_limit_write_accepts_full_range(hass, mock_client, setup_integration, value):
    """The slider advertises 0-100; the boundary writes must reach the modbus setter
    without raising. givenergy-modbus <2.5.8 rejected >50, so this guards the
    cross-repo write contract that the pin (>=2.5.8) now satisfies (#52)."""
    entity_id = _entity_id(hass, "SA1234G123_battery_charge_limit")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


@pytest.mark.parametrize("value", [0, 100])
async def test_ac_limit_write_accepts_full_range(hass, mock_client, ac_coupled_setup, value):
    """The AC slider dropped its 1-floor to 0-100; both boundaries must reach the
    modbus AC setter without raising (#52, modbus #301/#302)."""
    entity_id = _entity_id(hass, "SA1234G123_battery_charge_limit_ac")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


@pytest.fixture
async def aio_setup(hass, mock_client, mock_plant, mock_inverter, mock_config_entry):
    """Set up the integration with a single-phase All-in-One plant.

    AIO exposes the AC-config register block (HR300+) despite not being AC-coupled,
    so the AC limits must be created — gated on has_ac_config_block, not is_ac_coupled.
    """
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.ALL_IN_ONE,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.battery_charge_limit_ac = 50
    mock_inverter.battery_discharge_limit_ac = 60
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_ac_limits_present_on_all_in_one_plant(hass, aio_setup):
    """AIO exposes the AC-config block, so the AC limits must be created."""
    _entity_id(hass, "SA1234G123_battery_charge_limit_ac")
    _entity_id(hass, "SA1234G123_battery_discharge_limit_ac")
