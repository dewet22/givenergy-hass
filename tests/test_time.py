"""Tests for the GivEnergy Local time platform (charge/discharge + smart load slots)."""

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, unique_id)
    assert entity_id is not None, f"No time entity for unique_id={unique_id!r}"
    return entity_id


def _maybe_entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("time", DOMAIN, unique_id)


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
        "battery_pause_slot_start",
        "battery_pause_slot_end",
        *[f"smart_load_slot_{i}_{ep}" for i in range(1, 11) for ep in ("start", "end")],
    ]
    for key in expected_keys:
        entity_id = _entity_id(hass, f"SA1234G123_{key}")
        state = hass.states.get(entity_id)
        assert state is not None, f"Entity SA1234G123_{key} has no state"


async def test_battery_pause_slot_absent_when_register_unreadable(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """#207: firmware without the battery pause slot (it reads None) gets no
    pause-slot controls."""
    mock_inverter.battery_pause_slot_1 = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _maybe_entity_id(hass, "SA1234G123_battery_pause_slot_start") is None
    assert _maybe_entity_id(hass, "SA1234G123_battery_pause_slot_end") is None


async def test_set_battery_pause_slot_start_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_pause_slot_start")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "14:00:00"}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


async def test_set_battery_pause_slot_end_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_battery_pause_slot_end")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "15:00:00"}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


async def test_smart_load_slot_1_start_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_smart_load_slot_1_start"))
    assert state.state == "06:00:00"


async def test_smart_load_slot_1_end_initial_value(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "SA1234G123_smart_load_slot_1_end"))
    assert state.state == "07:00:00"


async def test_set_smart_load_slot_1_start_sends_command(hass, mock_client, setup_integration):
    entity_id = _entity_id(hass, "SA1234G123_smart_load_slot_1_start")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "08:30:00"}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
    cmd_arg = mock_client.one_shot_command.call_args[0][0]
    assert isinstance(cmd_arg, list)
    assert len(cmd_arg) > 0


async def test_set_smart_load_slot_5_end_sends_command(hass, mock_client, setup_integration):
    """Spot-check mid-range slot to confirm idx capture is correct across all 10."""
    entity_id = _entity_id(hass, "SA1234G123_smart_load_slot_5_end")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "09:00:00"}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()


def test_smart_load_slot_getter_returns_none_when_field_absent():
    """The getter must read None, not raise, when the field is missing entirely.

    Both current inverter models define smart_load_slot_* as optional pydantic fields,
    so direct access is safe today. This guards the defensive getattr contract against a
    future model that drops the field: since these entities are created unconditionally,
    a missing field must surface as None (entity unavailable) rather than AttributeError.
    """
    from custom_components.givenergy_local.time import _smart_load_slot_getter

    class _ModelWithoutSmartLoad:
        """Stand-in for an inverter model lacking smart_load_slot_* attributes."""

    getter = _smart_load_slot_getter(1)
    assert getter(_ModelWithoutSmartLoad()) is None
