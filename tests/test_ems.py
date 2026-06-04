"""Tests for the EMS plant-level scheduling entities (issue #74).

These are only created when the plant is an EMS (coordinator.data.ems is not
None); the shared fixtures default ems to None, so here we override it with a
mock Ems before setting up the integration.
"""

from unittest.mock import MagicMock

import pytest
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.inverter import Model
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.givenergy_local.const import DOMAIN


@pytest.fixture
def mock_ems() -> MagicMock:
    ems = MagicMock()
    ems.charge_slot_1 = TimeSlot.from_components(2, 0, 5, 0)
    ems.charge_slot_2 = None
    ems.charge_slot_3 = None
    ems.discharge_slot_1 = TimeSlot.from_components(17, 0, 19, 0)
    ems.discharge_slot_2 = None
    ems.discharge_slot_3 = None
    ems.export_slot_1 = TimeSlot.from_components(10, 0, 16, 0)
    ems.export_slot_2 = None
    ems.export_slot_3 = None
    ems.charge_target_1 = 80
    ems.charge_target_2 = 100
    ems.charge_target_3 = 100
    ems.discharge_target_1 = 20
    ems.discharge_target_2 = 4
    ems.discharge_target_3 = 4
    ems.export_target_1 = 100
    ems.export_target_2 = 4
    ems.export_target_3 = 4
    ems.export_power_limit = 3600
    ems.plant_enabled = True
    return ems


@pytest.fixture
async def ems_setup(hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry):
    """Set up the integration with the plant presenting as an EMS."""
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS  # an EMS plant's controller decodes as Model.EMS
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


def _entity_id(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


# ---------------------------------------------------------------------------
# Creation gating
# ---------------------------------------------------------------------------


async def test_ems_entities_created_for_ems_plant(hass, ems_setup):
    """EMS plant exposes 18 slot-time + 10 number + 1 switch entities."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, ems_setup.entry_id)
    ems_times = [e for e in entries if e.domain == "time" and "_ems_" in e.unique_id]
    ems_numbers = [e for e in entries if e.domain == "number" and "_ems_" in e.unique_id]
    ems_switches = [e for e in entries if e.domain == "switch" and "_ems_" in e.unique_id]
    assert len(ems_times) == 18  # charge+discharge+export x slots 1-3 x start/end
    assert len(ems_numbers) == 10  # 9 slot SoC targets + export power limit
    assert len(ems_switches) == 1  # Flexi EMS Control


async def test_no_ems_entities_for_non_ems_plant(hass, setup_integration):
    """A plant without an EMS (the default) must not get EMS entities."""
    assert _entity_id(hass, "time", "SA1234G123_ems_charge_slot_1_start") is None
    assert _entity_id(hass, "number", "SA1234G123_ems_charge_target_soc_1") is None
    assert _entity_id(hass, "number", "SA1234G123_ems_export_power_limit") is None


async def test_no_smart_load_entities_for_ems_plant(hass, ems_setup):
    """Smart Load slots are inverter-level and superseded by the EMS controller.

    The library only populates HR(554-573) on non-EMS inverters, so an EMS plant
    must not register them (else a block of unavailable config entities appears).
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, ems_setup.entry_id)
    smart_load = [e for e in entries if e.domain == "time" and "_smart_load_slot_" in e.unique_id]
    assert smart_load == []


# ---------------------------------------------------------------------------
# Initial values (read from coordinator.data.ems)
# ---------------------------------------------------------------------------


async def test_ems_charge_slot_initial_times(hass, ems_setup):
    start = hass.states.get(_entity_id(hass, "time", "SA1234G123_ems_charge_slot_1_start"))
    end = hass.states.get(_entity_id(hass, "time", "SA1234G123_ems_charge_slot_1_end"))
    assert start.state == "02:00:00"
    assert end.state == "05:00:00"


async def test_ems_discharge_slot_initial_times(hass, ems_setup):
    start = hass.states.get(_entity_id(hass, "time", "SA1234G123_ems_discharge_slot_1_start"))
    assert start.state == "17:00:00"


async def test_ems_charge_target_initial_value(hass, ems_setup):
    state = hass.states.get(_entity_id(hass, "number", "SA1234G123_ems_charge_target_soc_1"))
    assert float(state.state) == 80


async def test_ems_export_target_initial_value(hass, ems_setup):
    state = hass.states.get(_entity_id(hass, "number", "SA1234G123_ems_export_target_soc_1"))
    assert float(state.state) == 100


async def test_set_ems_export_target_soc_writes_to_ems_controller(hass, mock_client, ems_setup):
    entity_id = _entity_id(hass, "number", "SA1234G123_ems_export_target_soc_2")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 50}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
    (request,) = mock_client.one_shot_command.call_args[0][0]
    # EMS export target SoC writes go to the EMS controller (0x11) with the value.
    assert request.value == 50
    assert request.device_address == 0x11


async def test_ems_export_power_limit_initial_value(hass, ems_setup):
    state = hass.states.get(_entity_id(hass, "number", "SA1234G123_ems_export_power_limit"))
    assert float(state.state) == 3600
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 6000


async def test_set_ems_export_power_limit_writes_command(hass, mock_client, ems_setup):
    entity_id = _entity_id(hass, "number", "SA1234G123_ems_export_power_limit")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 2500}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
    (request,) = mock_client.one_shot_command.call_args[0][0]
    assert request.value == 2500
    assert request.device_address == 0x11


async def test_ems_export_slot_initial_times(hass, ems_setup):
    start = hass.states.get(_entity_id(hass, "time", "SA1234G123_ems_export_slot_1_start"))
    end = hass.states.get(_entity_id(hass, "time", "SA1234G123_ems_export_slot_1_end"))
    assert start.state == "10:00:00"
    assert end.state == "16:00:00"


async def test_flexi_ems_control_switch_reflects_plant_enabled(hass, ems_setup):
    state = hass.states.get(_entity_id(hass, "switch", "SA1234G123_ems_plant_enable"))
    assert state.state == "on"  # mock_ems.plant_enabled is True


async def test_flexi_ems_control_switch_writes_command(hass, mock_client, ems_setup):
    entity_id = _entity_id(hass, "switch", "SA1234G123_ems_plant_enable")
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    mock_client.one_shot_command.assert_called_once()


# ---------------------------------------------------------------------------
# Writes (build the right set_ems_* command)
# ---------------------------------------------------------------------------


async def test_set_ems_charge_slot_start_writes_endpoint(hass, mock_client, ems_setup):
    entity_id = _entity_id(hass, "time", "SA1234G123_ems_charge_slot_1_start")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entity_id, "time": "01:00:00"}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
    (request,) = mock_client.one_shot_command.call_args[0][0]
    # EMS charge slot 1 start = HR 2053, 01:00 -> 100, on the EMS controller 0x11.
    assert (request.register, request.value, request.device_address) == (2053, 100, 0x11)


async def test_set_ems_charge_target_soc_writes_register(hass, mock_client, ems_setup):
    entity_id = _entity_id(hass, "number", "SA1234G123_ems_charge_target_soc_1")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 75}, blocking=True
    )
    mock_client.one_shot_command.assert_called_once()
    (request,) = mock_client.one_shot_command.call_args[0][0]
    # EMS charge slot 1 target SoC = HR 2055.
    assert (request.register, request.value, request.device_address) == (2055, 75, 0x11)


# ---------------------------------------------------------------------------
# EMS device identity (name → givenergy_ems_ entity-id prefix) + realignment
# ---------------------------------------------------------------------------


async def test_ems_device_named_ems(hass, ems_setup):
    """An EMS controller gets its own device identity, so its entities slug to
    givenergy_ems_ rather than givenergy_inverter_."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SA1234G123")})
    assert device is not None
    assert device.name == "GivEnergy EMS SA1234G123"


async def test_no_realignment_issue_on_fresh_ems_install(hass, ems_setup):
    """A fresh EMS install already has givenergy_ems_ ids, so no recreate prompt."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"ems_entity_ids_outdated_{ems_setup.entry_id}"
    )
    assert issue is None


async def test_realignment_issue_raised_for_stale_inverter_prefixed_entities(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """An existing EMS install whose entities still carry the pre-rename
    givenergy_inverter_ prefix gets the 'Recreate entity IDs' repair issue."""
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)

    # Pre-seed a stale entity id (as a pre-rename install would have).
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "SA1234G123_status",
        config_entry=mock_config_entry,
        suggested_object_id="givenergy_inverter_sa1234g123_status",
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"ems_entity_ids_outdated_{mock_config_entry.entry_id}"
    )
    assert issue is not None
