"""Tests for the GivEnergy Local switch platform."""
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    assert entity_id is not None, f"No switch entity for unique_id={unique_id!r}"
    return entity_id


async def test_enable_charge_initial_state(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_charge"))
    assert state.state == "on"


async def test_enable_discharge_initial_state(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_enable_discharge"))
    assert state.state == "on"


async def test_turn_off_charge_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_enable_charge")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


async def test_turn_on_discharge_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_enable_discharge")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
