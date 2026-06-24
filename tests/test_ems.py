"""Tests for the EMS plant-level scheduling entities (issue #74).

These are only created when the plant is an EMS (coordinator.data.ems is not
None); the shared fixtures default ems to None, so here we override it with a
mock Ems before setting up the integration.
"""

from unittest.mock import MagicMock

import pytest
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.devices import InverterSummary
from givenergy_modbus.model.inverter import Model, Status
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
    ems.managed_inverters = []
    # Plant-level aggregate telemetry (#201) surfaced as EMS_SENSORS.
    ems.ems_status = Status.NORMAL
    ems.inverter_count = 2
    ems.calc_load_power = 1234
    ems.measured_load_power = 1300
    ems.grid_meter_power = -500
    ems.total_battery_power = 800
    ems.remaining_battery_wh = 5000
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


def _managed(serial: str, *, power=1000, soc=55, temp=31.5, status=None) -> InverterSummary:
    """Build a blinded managed-inverter rollup summary."""
    return InverterSummary(
        serial_number=serial,
        status=status,
        p_inverter_out=power,
        battery_soc=soc,
        t_inverter_heatsink=temp,
    )


@pytest.fixture
async def managed_ems_setup(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """An EMS plant whose controller fronts two managed inverters."""
    mock_ems.managed_inverters = [
        _managed("SA1111A001", power=1500, soc=60, temp=30.5),
        _managed("SA2222A002", power=-200, soc=45, temp=28.0),
    ]
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


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

    # Pre-seed a surviving entity (a coordinator diagnostic) under the stale
    # givenergy_inverter_ prefix. Inverter sensors are removed on EMS now (#201),
    # so the realignment prompt must key off an entity that actually persists.
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "SA1234G123_total_failures",
        config_entry=mock_config_entry,
        suggested_object_id="givenergy_inverter_sa1234g123_total_failures",
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"ems_entity_ids_outdated_{mock_config_entry.entry_id}"
    )
    assert issue is not None


# ---------------------------------------------------------------------------
# Managed-inverter child devices
# ---------------------------------------------------------------------------


async def test_managed_inverter_devices_created_and_parented(hass, managed_ems_setup):
    """Each managed inverter is its own device, parented to the EMS controller."""
    registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)
    controller = dev_registry.async_get_device(identifiers={(DOMAIN, "SA1234G123")})
    assert controller is not None
    for serial in ("SA1111A001", "SA2222A002"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_managed_power")
        assert entity_id is not None, f"{serial} has no power sensor"
        device = dev_registry.async_get(registry.async_get(entity_id).device_id)
        assert (DOMAIN, f"{serial}_managed") in device.identifiers
        assert device.name == f"GivEnergy Managed Inverter {serial}"
        assert device.model == "Managed Inverter (EMS)"
        assert device.via_device_id == controller.id


async def test_managed_inverter_values(hass, managed_ems_setup):
    """The blinded summary fields surface on the child sensors."""
    assert hass.states.get(_entity_id(hass, "sensor", "SA1111A001_managed_power")).state == "1500"
    assert (
        hass.states.get(_entity_id(hass, "sensor", "SA1111A001_managed_battery_soc")).state == "60"
    )
    temp = hass.states.get(_entity_id(hass, "sensor", "SA1111A001_managed_temperature"))
    assert float(temp.state) == 30.5
    assert temp.attributes["unit_of_measurement"] == "°C"
    assert hass.states.get(_entity_id(hass, "sensor", "SA2222A002_managed_power")).state == "-200"


async def test_managed_inverter_status_string_does_not_crash(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """#52: the EMS rollup reports each managed inverter's status as a raw string,
    not a Status enum. The status sensor must render it rather than blow up on
    `.name` (which raised AttributeError on every poll on real EMS hardware)."""
    mock_ems.managed_inverters = [_managed("SA1111A001", status="2")]
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, "sensor", "SA1111A001_managed_status"))
    assert state is not None
    assert state.state == "2"


async def test_managed_inverter_resolves_by_serial(hass, managed_ems_setup):
    """A managed inverter that drops out of the rollup goes unavailable while the
    survivor keeps reporting — entities track serial, not slot position."""
    coordinator = hass.data[DOMAIN][managed_ems_setup.entry_id]
    coordinator.data.ems.managed_inverters = [_managed("SA2222A002", power=-200, soc=45, temp=28.0)]
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()

    assert hass.states.get(_entity_id(hass, "sensor", "SA2222A002_managed_power")).state == "-200"
    assert (
        hass.states.get(_entity_id(hass, "sensor", "SA1111A001_managed_power")).state
        == "unavailable"
    )


async def test_no_managed_inverter_devices_for_non_ems_plant(hass, setup_integration):
    """A non-EMS plant gets no managed-inverter devices."""
    dev_registry = dr.async_get(hass)
    managed = [
        d
        for d in dr.async_entries_for_config_entry(dev_registry, setup_integration.entry_id)
        if d.model == "Managed Inverter (EMS)"
    ]
    assert managed == []


async def test_managed_inverter_dedup_duplicate_serial(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """Two rollup slots reporting the same serial yield a single device."""
    mock_ems.managed_inverters = [_managed("SA3333A003"), _managed("SA3333A003", power=99)]
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    dev_registry = dr.async_get(hass)
    managed = [
        d
        for d in dr.async_entries_for_config_entry(dev_registry, mock_config_entry.entry_id)
        if d.model == "Managed Inverter (EMS)"
    ]
    assert len(managed) == 1


async def test_managed_inverter_namespaced_against_same_serial(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """#203: a managed-inverter rollup must not hijack a same-serial device.

    The rollup here reports the controller's own serial — the in-one-entry repro of
    the collision (a separately-configured direct inverter of a managed serial is
    the cross-entry form). Pre-fix this merged the rollup's DeviceInfo onto
    `(DOMAIN, serial)` and dropped the colliding status/SoC sensors; the `_managed`
    namespace keeps the two identities distinct.
    """
    mock_ems.managed_inverters = [_managed("SA1234G123", power=1500, soc=60, temp=30.5)]
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)

    # The controller keeps its bare-serial device identity and its own status sensor.
    controller = dev_registry.async_get_device(identifiers={(DOMAIN, "SA1234G123")})
    assert controller is not None
    assert controller.model != "Managed Inverter (EMS)"
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_status") is not None

    # The rollup gets a distinct `_managed` device + namespaced sensors — no collision.
    managed = dev_registry.async_get_device(identifiers={(DOMAIN, "SA1234G123_managed")})
    assert managed is not None
    assert managed.id != controller.id
    assert managed.model == "Managed Inverter (EMS)"
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_managed_status") is not None
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_managed_battery_soc") is not None
    )


# ---------------------------------------------------------------------------
# Inverter-entity suppression on EMS (#201)
# ---------------------------------------------------------------------------


async def test_inverter_sensors_kept_on_ems_except_local_load(hass, ems_setup):
    """The EMS controller's 0x11 block carries real plant data (PV/grid/battery/AC),
    so the inverter sensors stay (#201). Only the controller-local load figures —
    House Consumption and the inverter busbar Load Power — are gated off via
    skip_if_ems; the EMS load aggregates supersede them."""
    assert _entity_id(hass, "sensor", "SA1234G123_status") is not None
    assert _entity_id(hass, "sensor", "SA1234G123_battery_soc") is not None
    assert _entity_id(hass, "sensor", "SA1234G123_e_consumption_today") is None
    assert _entity_id(hass, "sensor", "SA1234G123_p_load_demand") is None


async def test_inverter_controls_suppressed_on_ems_plant(hass, ems_setup):
    """Inverter-level controls are redundant on an EMS plant — the EMS slots are
    authoritative — so they're suppressed across switch/number/select/time."""
    assert _entity_id(hass, "switch", "SA1234G123_enable_charge") is None
    assert _entity_id(hass, "number", "SA1234G123_charge_target_soc") is None
    assert _entity_id(hass, "select", "SA1234G123_battery_power_mode") is None
    assert _entity_id(hass, "time", "SA1234G123_charge_slot_1_start") is None


async def test_upgrade_narrows_retired_entities_on_ems(
    hass, mock_client, mock_plant, mock_inverter, mock_ems, mock_config_entry
):
    """Upgrade path (#201): on an existing EMS entry the reconciliation removes only
    the inverter-level controls, the EMS-gated House Consumption, and the dropped
    ems_status aggregate — the meaningful inverter sensors and diagnostics survive."""
    mock_plant.ems = mock_ems
    mock_inverter.model = Model.EMS
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Rows a prior install would carry: retired (controls + broken derived sensor +
    # dropped aggregate) and retained (a real inverter sensor + a diagnostic).
    for domain, unique_id in (
        ("switch", "SA1234G123_enable_charge"),
        ("number", "SA1234G123_charge_target_soc"),
        ("select", "SA1234G123_battery_power_mode"),
        ("time", "SA1234G123_charge_slot_1_start"),
        ("sensor", "SA1234G123_e_consumption_today"),
        ("sensor", "SA1234G123_ems_status"),
        ("sensor", "SA1234G123_status"),
        ("sensor", "SA1234G123_total_failures"),
    ):
        registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=mock_config_entry)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Retired: inverter controls, the broken derived sensor, the dropped aggregate.
    assert _entity_id(hass, "switch", "SA1234G123_enable_charge") is None
    assert _entity_id(hass, "number", "SA1234G123_charge_target_soc") is None
    assert _entity_id(hass, "select", "SA1234G123_battery_power_mode") is None
    assert _entity_id(hass, "time", "SA1234G123_charge_slot_1_start") is None
    assert _entity_id(hass, "sensor", "SA1234G123_e_consumption_today") is None
    assert _entity_id(hass, "sensor", "SA1234G123_ems_status") is None
    # Retained: meaningful inverter sensor + coordinator diagnostic.
    assert _entity_id(hass, "sensor", "SA1234G123_status") is not None
    assert _entity_id(hass, "sensor", "SA1234G123_total_failures") is not None


# ---------------------------------------------------------------------------
# EMS plant-level telemetry sensors (#201)
# ---------------------------------------------------------------------------


async def test_ems_sensors_created_and_read(hass, ems_setup):
    """The EMS aggregate telemetry surfaces on the controller device."""
    assert hass.states.get(_entity_id(hass, "sensor", "SA1234G123_ems_inverter_count")).state == "2"
    grid = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_ems_grid_meter_power"))
    assert grid.state == "-500"
    assert grid.attributes["unit_of_measurement"] == "W"
    assert grid.attributes["device_class"] == "power"
    remaining = hass.states.get(
        _entity_id(hass, "sensor", "SA1234G123_ems_remaining_battery_energy")
    )
    assert remaining.state == "5000"


async def test_no_ems_sensors_for_non_ems_plant(hass, setup_integration):
    """A non-EMS plant must not get the EMS aggregate sensors."""
    assert _entity_id(hass, "sensor", "SA1234G123_ems_grid_meter_power") is None
    assert _entity_id(hass, "sensor", "SA1234G123_ems_inverter_count") is None


def test_ems_measured_load_power_hidden_by_default():
    """#52: EMS Measured Load Power reads a constant zero on current firmware, so
    it ships hidden (still recorded) in favour of the calculated-load aggregate."""
    from custom_components.givenergy_local.sensor import EMS_SENSORS

    desc = next(d for d in EMS_SENSORS if d.key == "ems_measured_load_power")
    assert desc.entity_registry_visible_default is False


def test_controller_local_load_gated_skip_if_ems():
    """Exactly the controller-local load figures are gated off on EMS — House
    Consumption and the inverter busbar Load Power. Pins the boundary so a future
    change can't silently widen or narrow it; the gate drops them only on EMS."""
    from custom_components.givenergy_local.sensor import (
        INVERTER_SENSORS,
        _include_inverter_sensor,
    )

    gated = {d.key for d in INVERTER_SENSORS if d.skip_if_ems}
    assert gated == {"e_consumption_today", "p_load_demand"}
    desc = next(d for d in INVERTER_SENSORS if d.key == "p_load_demand")
    inv = MagicMock()
    assert _include_inverter_sensor(desc, inv, False, False, True) is False
    assert _include_inverter_sensor(desc, inv, False, False, False) is True
