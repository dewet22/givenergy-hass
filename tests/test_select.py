"""Tests for the GivEnergy Local select platform."""

import pytest
from givenergy_modbus.model.battery import ExportPriority
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
    assert entity_id is not None, f"No select entity for unique_id={unique_id!r}"
    return entity_id


def _maybe_entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("select", DOMAIN, unique_id)


async def test_battery_power_mode_initial_option(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_power_mode"))
    assert state.state == "Self Consumption"


async def test_battery_power_mode_options(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_power_mode"))
    assert set(state.attributes["options"]) == {"Export", "Self Consumption"}


async def test_select_export_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_power_mode")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Export"},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


async def test_select_self_consumption_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_power_mode")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Self Consumption"},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


async def test_battery_pause_mode_initial_option(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_pause_mode"))
    assert state.state == "Disabled"


async def test_battery_pause_mode_options(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_battery_pause_mode"))
    assert set(state.attributes["options"]) == {
        "Disabled",
        "Pause Charge",
        "Pause Discharge",
        "Pause Both",
    }


async def test_select_pause_both_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_pause_mode")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Pause Both"},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


# ---------------------------------------------------------------------------
# AC-config-block controls — created for AC-coupled inverters and single-phase AIO
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
    mock_inverter.export_priority = ExportPriority.BATTERY_FIRST
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_export_priority_absent_on_hybrid_plant(hass, setup_integration):
    """The default fixture has device_type=Model.HYBRID — export priority must not be created."""
    assert _maybe_entity_id(hass, "SA1234G123_export_priority") is None


async def test_export_priority_present_on_ac_coupled_plant(hass, ac_coupled_setup):
    state = hass.states.get(_entity_id(hass, "SA1234G123_export_priority"))
    assert state is not None
    assert state.state == "Battery First"
    assert set(state.attributes["options"]) == {"Battery First", "Grid First", "Load First"}


async def test_select_export_priority_sends_command(hass, mock_client, ac_coupled_setup):
    entity_id = _entity_id(hass, "SA1234G123_export_priority")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Grid First"},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


@pytest.fixture
async def aio_setup(hass, mock_client, mock_plant, mock_inverter, mock_config_entry):
    """Set up the integration with a single-phase All-in-One plant.

    AIO exposes the AC-config register block (HR300+) despite not being AC-coupled,
    so export priority must be created — gated on has_ac_config_block, not is_ac_coupled.
    """
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.ALL_IN_ONE,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.export_priority = ExportPriority.BATTERY_FIRST
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_export_priority_present_on_all_in_one_plant(hass, aio_setup):
    """AIO exposes the AC-config block, so export priority must be created."""
    assert _maybe_entity_id(hass, "SA1234G123_export_priority") is not None
