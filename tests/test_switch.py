"""Tests for the GivEnergy Local switch platform."""

import pytest
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    assert entity_id is not None, f"No switch entity for unique_id={unique_id!r}"
    return entity_id


def _maybe_entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, unique_id)


async def test_enable_charge_initial_state(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_charge"))
    assert state.state == "on"


async def test_enable_discharge_initial_state(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_discharge"))
    assert state.state == "on"


async def test_turn_off_charge_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_enable_charge")
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    mock_client.one_shot_command.assert_called_once()


async def test_turn_on_discharge_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_enable_discharge")
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    mock_client.one_shot_command.assert_called_once()


async def test_enable_rtc_present_and_decodes(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_rtc"))
    assert state.state in ("on", "off")


async def test_toggle_rtc_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_enable_rtc")
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
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
    mock_inverter.enable_eps = False
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_enable_eps_absent_on_hybrid_plant(hass, setup_integration):
    """The default fixture has device_type=Model.HYBRID — EPS must not be created."""
    assert _maybe_entity_id(hass, "SA1234G123_enable_eps") is None


async def test_enable_eps_present_on_ac_coupled_plant(hass, ac_coupled_setup):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_eps"))
    assert state is not None
    assert state.state == "off"


async def test_turn_on_eps_sends_command(hass, mock_client, ac_coupled_setup):
    entity_id = _entity_id(hass, "SA1234G123_enable_eps")
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    mock_client.one_shot_command.assert_called_once()


@pytest.fixture
async def aio_setup(hass, mock_client, mock_plant, mock_inverter, mock_config_entry):
    """Set up the integration with a single-phase All-in-One plant.

    AIO exposes the AC-config register block (HR300+) despite not being AC-coupled,
    so EPS must be created — gated on has_ac_config_block, not is_ac_coupled.
    """
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.ALL_IN_ONE,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.enable_eps = False
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_enable_eps_present_on_all_in_one_plant(hass, aio_setup):
    """AIO exposes the AC-config block, so EPS must be created."""
    assert _maybe_entity_id(hass, "SA1234G123_enable_eps") is not None
