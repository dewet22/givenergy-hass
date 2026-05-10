"""Tests for the GivEnergy Local select platform."""

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
    assert entity_id is not None, f"No select entity for unique_id={unique_id!r}"
    return entity_id


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
