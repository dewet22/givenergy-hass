"""Tests for the GivEnergy Local number platform."""

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
    assert entity_id is not None, f"No number entity for unique_id={unique_id!r}"
    return entity_id


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
