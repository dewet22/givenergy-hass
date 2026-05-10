"""Tests for the GivEnergy Local time platform (charge/discharge slots)."""

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, unique_id)
    assert entity_id is not None, f"No time entity for unique_id={unique_id!r}"
    return entity_id


async def test_charge_slot_1_start_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_charge_slot_1_start"))
    assert state.state == "00:30:00"


async def test_charge_slot_1_end_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_charge_slot_1_end"))
    assert state.state == "04:30:00"


async def test_discharge_slot_1_start_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_discharge_slot_1_start"))
    assert state.state == "17:00:00"


async def test_set_charge_slot_1_start_builds_correct_timeslot(
    hass, mock_client, setup_integration, mock_inverter
):
    entity_id = _entity_id(hass, "SA1234G123_charge_slot_1_start")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "01:00:00"}, blocking=True
    )

    mock_client.one_shot_command.assert_called_once()
    # The command should have been built with the new start and the existing end (04:30)
    cmd_arg = mock_client.one_shot_command.call_args[0][0]
    assert isinstance(cmd_arg, list)
    assert len(cmd_arg) > 0


async def test_set_charge_slot_1_end_preserves_start(
    hass, mock_client, setup_integration, mock_inverter
):
    entity_id = _entity_id(hass, "SA1234G123_charge_slot_1_end")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "06:00:00"}, blocking=True
    )

    mock_client.one_shot_command.assert_called_once()
    cmd_arg = mock_client.one_shot_command.call_args[0][0]
    assert isinstance(cmd_arg, list)
    assert len(cmd_arg) > 0


async def test_all_time_slot_entities_created(hass, setup_integration):
    expected_keys = [
        "charge_slot_1_start",
        "charge_slot_1_end",
        "charge_slot_2_start",
        "charge_slot_2_end",
        "discharge_slot_1_start",
        "discharge_slot_1_end",
        "discharge_slot_2_start",
        "discharge_slot_2_end",
    ]
    for key in expected_keys:
        entity_id = _entity_id(hass, f"SA1234G123_{key}")
        state = hass.states.get(entity_id)
        assert state is not None, f"Entity SA1234G123_{key} has no state"
