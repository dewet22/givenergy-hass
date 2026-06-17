"""Tests for the GivEnergy Local sensor platform."""

from datetime import UTC
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN
from custom_components.givenergy_local.sensor import (
    AIO_MODULE_SENSORS,
    BATTERY_SENSORS,
    COORDINATOR_SENSORS,
    INVERTER_SENSORS,
    GivEnergyInverterSensorDescription,
    _include_inverter_sensor,
)


def _inverter_desc(key: str):
    return next(d for d in INVERTER_SENSORS if d.key == key)


def test_enum_value_fns_tolerate_none_attribute():
    """value_fns reading `.name` off an enum attribute must return None, not crash,
    when the attribute is None — the library serves an empty model (all attrs None)
    during partial / pre-first-poll windows (issue #52)."""
    empty = MagicMock()
    for key in (
        "status",
        "meter_type",
        "battery_type",
        "battery_calibration_stage",
        "usb_device_inserted",
        "battery_maintenance_mode",
    ):
        setattr(empty, key, None)
        assert _inverter_desc(key).value_fn(empty) is None, f"{key} value_fn crashed on None"


def test_status_value_fn_renders_when_present():
    """Sanity: the guarded status value_fn still renders a real status."""
    inv = MagicMock()
    inv.status.name = "NORMAL"
    assert _inverter_desc("status").value_fn(inv) == "normal"


def test_inverter_value_fns_resolve_against_real_model():
    """Every INVERTER_SENSORS value_fn must resolve against a REAL inverter model,
    not just the MagicMock fixture (which fabricates any attribute and so masks
    field drift vs givenergy-modbus). Regression for the rc8 breakage where the
    library renamed battery-energy fields (#76) and the skip_if_none setup filter
    raised AttributeError, taking down the whole sensor platform.

    Mirrors the eager filter in sensor.async_setup_entry, then exercises every
    value_fn (the native_value path) — both must run without raising on a
    cold, all-None model.
    """
    from givenergy_modbus.model.inverter import SinglePhaseInverter

    inv = SinglePhaseInverter()  # all fields None, like a pre-first-poll model

    # The setup-time filter evaluates skip_if_none value_fns eagerly; a field the
    # library no longer exposes would raise AttributeError here.
    [d for d in INVERTER_SENSORS if not d.skip_if_none or d.value_fn(inv) is not None]

    # And the native_value path for every sensor must resolve too.
    for d in INVERTER_SENSORS:
        d.value_fn(inv)


def test_setup_filter_skips_bad_descriptor_instead_of_crashing():
    """A skip_if_none descriptor whose value_fn raises must be skipped (logged),
    not propagate — so one bad descriptor can't take down the whole platform the
    way the renamed-field bug did. A healthy descriptor is still included."""

    def _boom(_inv):
        raise AttributeError("field renamed out from under us")

    bad = GivEnergyInverterSensorDescription(
        key="bad", name="Bad", value_fn=_boom, skip_if_none=True
    )
    good = GivEnergyInverterSensorDescription(
        key="good", name="Good", value_fn=lambda _inv: 1.0, skip_if_none=True
    )
    plain = GivEnergyInverterSensorDescription(
        key="plain", name="Plain", value_fn=_boom, skip_if_none=False
    )

    inv = MagicMock()
    assert _include_inverter_sensor(bad, inv, False) is False  # skipped, no raise
    assert _include_inverter_sensor(good, inv, False) is True
    # non-skip descriptors aren't evaluated at setup, so they're always included
    assert _include_inverter_sensor(plain, inv, False) is True


def test_single_phase_only_sensors_gated_on_three_phase():
    """single_phase_only descriptions are dropped on three-phase plants but kept on
    single-phase — the single-phase-only PV/capacity fields would otherwise surface as
    permanently-unavailable orphan entities on three-phase inverters (#94), and
    t_battery would read a frozen, unpopulated single-phase register there (#174)."""
    inv = MagicMock()
    gated_keys = {"p_pv", "e_pv_day", "battery_capacity_kwh", "t_battery"}

    # Every gated key must actually carry the flag (guards against silent drift if
    # one is renamed/removed) ...
    flagged = {d.key for d in INVERTER_SENSORS if d.single_phase_only}
    assert flagged == gated_keys

    # ... is excluded on three-phase ...
    for key in gated_keys:
        assert _include_inverter_sensor(_inverter_desc(key), inv, True) is False
    # ... and retained on single-phase.
    for key in gated_keys:
        assert _include_inverter_sensor(_inverter_desc(key), inv, False) is True


def test_device_kind_buckets_to_inverter_ems_gateway():
    """Device-name noun (which drives the entity_id prefix) buckets correctly —
    every actual inverter stays "Inverter"; EMS/Gateway get their own identity."""
    from givenergy_modbus.model.inverter import Model

    from custom_components.givenergy_local.sensor import _device_kind

    assert _device_kind(Model.EMS) == "EMS"
    assert _device_kind(Model.GATEWAY) == "Gateway"
    for m in (Model.HYBRID, Model.AC, Model.AC_3PH, Model.ALL_IN_ONE, Model.HYBRID_3PH):
        assert _device_kind(m) == "Inverter"


def _entity_id(hass, platform: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"No entity found for unique_id={unique_id!r}"
    return entity_id


def _suggested_precision(hass, unique_id: str) -> int | None:
    """The sensor's suggested display precision, as stored in the entity registry."""
    registry = er.async_get(hass)
    entry = registry.async_get(_entity_id(hass, "sensor", unique_id))
    return entry.options.get("sensor", {}).get("suggested_display_precision")


async def test_expected_sensor_count(hass, setup_integration):
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_integration.entry_id)
    sensors = [e for e in entries if e.domain == "sensor"]
    # 1 battery → inverter sensors + battery sensors + coordinator diagnostics,
    # minus e_load_total which only exists on three-phase models (#154).
    expected = len(INVERTER_SENSORS) + len(BATTERY_SENSORS) + len(COORDINATOR_SENSORS) - 1
    assert len(sensors) == expected


async def test_single_phase_only_sensors_absent_on_three_phase(
    hass, mock_client, mock_config_entry
):
    """On a three-phase plant the single-phase-only PV/capacity/battery-temperature
    sensors must not be created — the PV/capacity ones would render as
    permanently-unavailable orphans (#94), and t_battery reads a frozen, unpopulated
    single-phase register on three-phase (#174). The default (single-phase) fixture
    keeps them, asserted by test_expected_sensor_count.
    """
    from givenergy_modbus.model.inverter import Model
    from givenergy_modbus.model.plant import PlantCapabilities

    mock_client.plant.capabilities = PlantCapabilities(
        device_type=Model.HYBRID_3PH,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for key in ("p_pv", "e_pv_day", "battery_capacity_kwh", "t_battery"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"SA1234G123_{key}") is None, (
            f"{key} should be suppressed on three-phase"
        )


async def test_pv_power_sensor(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_p_pv"))
    assert state.state == "2500"
    assert state.attributes["unit_of_measurement"] == "W"


async def test_battery_soc_sensor(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_battery_soc"))
    assert state.state == "85"
    assert state.attributes["unit_of_measurement"] == "%"


async def test_grid_power_sensor_negative_is_import(hass, setup_integration):
    # p_grid_out is negative when importing from grid
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_grid_power"))
    assert float(state.state) == -800


async def test_work_time_total_reported_in_hours(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_work_time_total"))
    # Raw register is already in hours — no conversion applied (see sensor.py).
    assert float(state.state) == 36055
    assert state.attributes["unit_of_measurement"] == "h"


async def test_inverter_device_info(hass, setup_integration):
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_p_pv")
    entity_entry = registry.async_get(entry)

    dev_registry = dr.async_get(hass)
    device = dev_registry.async_get(entity_entry.device_id)
    assert device is not None
    assert device.manufacturer == "GivEnergy"
    assert device.serial_number == "SA1234G123"


async def test_battery_soc_per_battery_sensor(hass, setup_integration):
    # Battery sensor unique_id uses the battery serial, not the inverter serial
    state = hass.states.get(_entity_id(hass, "sensor", "BT1234A001_soc"))
    assert state.state == "85"


async def test_battery_device_linked_to_inverter(hass, setup_integration):
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id("sensor", DOMAIN, "BT1234A001_soc")
    entity_entry = registry.async_get(entry)

    dev_registry = dr.async_get(hass)
    battery_device = dev_registry.async_get(entity_entry.device_id)
    assert battery_device is not None
    # Battery device must be linked via the inverter device
    assert battery_device.via_device_id is not None


async def test_per_cell_voltage_sensors_created(hass, setup_integration):
    """All 16 per-cell voltage entities should be registered."""
    registry = er.async_get(hass)
    for i in range(1, 17):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"BT1234A001_v_cell_{i:02d}")
        assert entity_id is not None, f"Cell {i} voltage entity not registered"
        state = hass.states.get(entity_id)
        assert state.attributes["unit_of_measurement"] == "V"


async def test_cell_voltage_value(hass, setup_integration):
    """Cell voltages report the underlying battery value verbatim."""
    state = hass.states.get(_entity_id(hass, "sensor", "BT1234A001_v_cell_01"))
    # mock_battery sets v_cell_01 to 3.275 + 1*0.001 = 3.276
    assert float(state.state) == 3.276


async def test_cell_temperature_groups_created(hass, setup_integration):
    """All 4 cell-group temperature entities should be registered."""
    registry = er.async_get(hass)
    for a, b in [(1, 4), (5, 8), (9, 12), (13, 16)]:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"BT1234A001_t_cells_{a:02d}_{b:02d}"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state.attributes["unit_of_measurement"] == "°C"


async def test_bms_internal_sensors_present(hass, setup_integration):
    """BMS MOSFET temperature, cell voltage sum, and cell count are exposed."""
    assert hass.states.get(_entity_id(hass, "sensor", "BT1234A001_t_bms_mosfet")).state == "28.4"
    assert (
        float(hass.states.get(_entity_id(hass, "sensor", "BT1234A001_v_cells_sum")).state) == 52.412
    )
    assert hass.states.get(_entity_id(hass, "sensor", "BT1234A001_num_cells")).state == "16"


async def test_bms_diagnostic_sensors_present(hass, setup_integration):
    """The bms_firmware_version, cap_design2 and usb_device_inserted sensors are exposed."""
    assert (
        hass.states.get(_entity_id(hass, "sensor", "BT1234A001_bms_firmware_version")).state
        == "3005"
    )
    cap_alt = hass.states.get(_entity_id(hass, "sensor", "BT1234A001_cap_design2"))
    assert float(cap_alt.state) == 9.5
    assert cap_alt.attributes["unit_of_measurement"] == "Ah"
    # usb_device_inserted is rendered as 4-char hex (uint16 value).
    assert (
        hass.states.get(_entity_id(hass, "sensor", "BT1234A001_usb_device_inserted")).state
        == "0x0008"
    )


async def test_bms_status_warning_rendered_as_hex(hass, setup_integration):
    """BMS status / warning bytes surface as 2-char hex strings, not decimals."""
    # status_3 is set to 0xA5 in the fixture; the rest are 0x00.
    assert hass.states.get(_entity_id(hass, "sensor", "BT1234A001_status_3")).state == "0xA5"
    assert hass.states.get(_entity_id(hass, "sensor", "BT1234A001_status_1")).state == "0x00"
    assert hass.states.get(_entity_id(hass, "sensor", "BT1234A001_warning_1")).state == "0x00"
    # Hex-formatted sensors deliberately omit state_class so HA doesn't
    # try to roll them into long-term statistics.
    state = hass.states.get(_entity_id(hass, "sensor", "BT1234A001_status_3"))
    assert "state_class" not in state.attributes


async def test_cell_voltages_attached_to_battery_device(hass, setup_integration):
    """Per-cell sensors live on the battery device, not the inverter device."""

    registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "BT1234A001_v_cell_01")
    entity_entry = registry.async_get(entity_id)
    device = dev_registry.async_get(entity_entry.device_id)
    assert device.serial_number == "BT1234A001"


async def test_new_inverter_sensors_present(hass, setup_integration):
    """Spot-check a handful of the newly-added inverter sensors."""
    cases = {
        "system_mode": "1",
        "charge_status": "charging",
        "i_ac1": "5.2",
        "p_combined_generation": "2500",
        "p_backup": "0",
        "num_phases": "1",
        "num_mppt": "2",
        "arm_firmware_version": "449",
    }
    for key, expected in cases.items():
        state = hass.states.get(_entity_id(hass, "sensor", f"SA1234G123_{key}"))
        assert state.state == expected, f"{key}: expected {expected!r}, got {state.state!r}"


async def test_enum_sensors_render_as_human_readable(hass, setup_integration):
    """meter_type and battery_type enums should surface as lowercase enum keys, not ints."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_battery_type"))
    assert state.state == "lithium"
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_meter_type"))
    assert state.state == "ct_or_em418"


async def test_battery_capacity_sensors(hass, setup_integration):
    """Battery capacity is exposed both in Ah and as the computed kWh field."""
    ah = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_battery_capacity_ah"))
    assert ah.state == "160"
    assert ah.attributes["unit_of_measurement"] == "Ah"

    kwh = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_battery_capacity_kwh"))
    assert float(kwh.state) == 8.19
    assert kwh.attributes["unit_of_measurement"] == "kWh"


async def test_consecutive_failures_starts_at_zero(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_consecutive_failures"))
    assert state.state == "0"


async def test_last_successful_refresh_set_after_setup(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_last_successful_refresh"))
    # After a successful first refresh the timestamp should be populated
    assert state.state not in ("unknown", "unavailable")


async def test_diagnostic_sensors_available_during_coordinator_failure(
    hass, mock_client, mock_config_entry
):
    """Coordinator diagnostic sensors must remain available even when updates fail."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Make subsequent refreshes time out
    mock_client.refresh.side_effect = TimeoutError()
    from custom_components.givenergy_local.const import DOMAIN as _DOMAIN

    coordinator = hass.data[_DOMAIN][mock_config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    failures_id = registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_consecutive_failures")
    refresh_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "SA1234G123_last_successful_refresh"
    )

    assert hass.states.get(failures_id).state == "1"
    assert hass.states.get(refresh_id).state != "unavailable"


async def test_partial_failures_starts_at_zero(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_partial_failures"))
    assert state.state == "0"


def test_partial_failure_attributes_name_device_and_time():
    """The diagnostic attributes name the dropped bank, count, per-bank detail and
    when it last happened — retained past a clean poll so an intermittent failure
    stays diagnosable (#176)."""
    from datetime import datetime

    from givenergy_modbus.exceptions import ReadFailure

    from custom_components.givenergy_local.sensor import _partial_failure_attributes

    coordinator = MagicMock()
    coordinator.last_partial_failures = [
        ReadFailure(
            device_address=0x34,
            request_type="ReadInputRegisters",
            base_register=60,
            register_count=60,
        )
    ]
    stamp = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
    coordinator.last_partial_at = stamp

    attrs = _partial_failure_attributes(coordinator)
    assert attrs["last_failed_devices"] == ["0x34"]
    assert attrs["last_failure_count"] == 1
    assert attrs["last_failures"] == ["0x34 ReadInputRegisters @ 60+60"]
    assert attrs["last_partial_at"] == stamp.isoformat()

    # No failures recorded → no attributes.
    coordinator.last_partial_failures = []
    assert _partial_failure_attributes(coordinator) is None


async def test_partial_failures_increments_and_attributes_name_device(
    hass, mock_client, mock_plant, mock_config_entry
):
    """After a partial poll, the sensor increments and its attributes name the
    device(s) that dropped — the only UI signal of a flaky device, since its
    entities stay available with stale data."""
    from givenergy_modbus.exceptions import ReadFailure, RefreshPartiallySucceeded

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # A steady-state partial: battery 0x34's input bank dropped.
    mock_client.refresh.side_effect = RefreshPartiallySucceeded(
        "partial",
        plant=mock_plant,
        failures=[
            ReadFailure(
                device_address=0x34,
                request_type="ReadInputRegisters",
                base_register=60,
                register_count=60,
            )
        ],
        cause=ExceptionGroup("reads", [TimeoutError()]),
    )

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_partial_failures"))
    assert state.state == "1"
    assert "0x34" in state.attributes["last_failed_devices"]
    assert state.attributes["last_failure_count"] == 1


# ---------------------------------------------------------------------------
# Native display precision (derived from the library's register scaling)
# ---------------------------------------------------------------------------


async def test_native_precision_derived_for_register_backed_sensors(hass, setup_integration):
    """Register-backed numeric sensors get the decimals implied by their scaling."""
    assert _suggested_precision(hass, "BT1234A001_v_cell_01") == 3  # milli
    assert _suggested_precision(hass, "BT1234A001_t_bms_mosfet") == 1  # deci
    assert _suggested_precision(hass, "BT1234A001_cap_design2") == 2  # uint32 -> centi
    assert _suggested_precision(hass, "BT1234A001_soc") == 0  # uint16


async def test_string_rendered_sensor_has_no_precision(hass, setup_integration):
    """usb_device_inserted is a uint16 register rendered as a hex string; it must
    NOT get a display precision (no state_class) or HA would mis-format it."""
    assert _suggested_precision(hass, "BT1234A001_usb_device_inserted") is None


async def test_computed_sensors_use_explicit_precision(hass, setup_integration):
    """Computed (non-register-backed) sensors pin precision in their descriptor."""
    assert _suggested_precision(hass, "SA1234G123_e_pv_day") == 1
    assert _suggested_precision(hass, "SA1234G123_battery_capacity_kwh") == 2
    assert _suggested_precision(hass, "SA1234G123_p_pv") == 0


# --- givenergy-modbus #174: consumption sensor + e_load_day rename + migration ---


async def test_house_consumption_today_sensor(hass, setup_integration):
    """The new derived consumption sensor (the dashboard's real 'Consumed')."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_consumption_today"))
    assert state.state == "21.4"


async def test_house_consumption_today_uses_native_register_on_three_phase(
    hass, mock_client, mock_config_entry
):
    """Three-phase inverters meter consumption directly (e_load_today, IR 1396-1397),
    so the same entity key reads the native register there instead of the derived
    single-phase field — dashboards keyed on e_consumption_today keep working (#154).
    The native lifetime counter surfaces alongside it as e_load_total."""
    from givenergy_modbus.model.inverter import Model
    from givenergy_modbus.model.plant import PlantCapabilities

    inv = mock_client.plant.inverter
    del inv.e_consumption_today  # derived field exists only on single-phase models
    inv.e_load_today = 6.2
    inv.e_load_total = 1234.5
    mock_client.plant.capabilities = PlantCapabilities(
        device_type=Model.HYBRID_3PH,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_consumption_today"))
    assert state.state == "6.2"
    total = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_load_total"))
    assert total.state == "1234.5"


async def test_house_consumption_total_absent_on_single_phase(hass, setup_integration):
    """Single-phase units expose no native lifetime consumption register, so the
    e_load_total sensor must not be created there."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_e_load_total") is None


async def test_ac_charge_today_sensor_replaces_load_energy(hass, setup_integration):
    """e_load_day was a mislabel (it's AC charge); the renamed sensor reads it, and
    nothing remains registered under the old unique_id."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_ac_charge_today"))
    assert state.state == "3.8"
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_e_load_day") is None


async def test_unique_id_migration_repoints_e_load_day(hass, mock_client, mock_config_entry):
    """A pre-2.1.1 entity under the old unique_id is re-pointed in place on setup —
    same entity_id (so history/stats survive), new unique_id, old uid gone."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_e_load_day", config_entry=mock_config_entry
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    migrated = registry.async_get(old.entity_id)
    assert migrated is not None, "entity_id was not preserved across the migration"
    assert migrated.unique_id == "SA1234G123_e_ac_charge_today"
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_e_load_day") is None


# --- givenergy-modbus #174/#176: inverter-output pair renamed to PV generation ---


async def test_pv_generation_today_sensor(hass, setup_integration):
    """IR44 is PV generation (not inverter AC output); entity renamed accordingly."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_pv_generation_today"))
    assert state.state == "11.2"


async def test_pv_generation_total_sensor(hass, setup_integration):
    """IR45/46 is PV generation total; entity renamed from 'Inverter Output Total'."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_e_pv_generation_total"))
    assert state.state == "5100.2"


async def test_inverter_output_old_uids_gone(hass, setup_integration):
    """Old unique_ids must be absent — they've been migrated to the PV generation names."""
    registry = er.async_get(hass)
    for old_uid in ("SA1234G123_e_inverter_out_day", "SA1234G123_e_inverter_out_total"):
        assert registry.async_get_entity_id("sensor", DOMAIN, old_uid) is None, (
            f"Old unique_id {old_uid!r} still registered — unique_id migration didn't run"
        )


async def test_unique_id_migration_repoints_inverter_output_pair(
    hass, mock_client, mock_config_entry
):
    """Pre-2.1.2 entities under the old inverter-output unique_ids are re-pointed in place."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_today = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_e_inverter_out_day", config_entry=mock_config_entry
    )
    old_total = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_e_inverter_out_total", config_entry=mock_config_entry
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for old_entry, expected_new_uid in (
        (old_today, "SA1234G123_e_pv_generation_today"),
        (old_total, "SA1234G123_e_pv_generation_total"),
    ):
        migrated = registry.async_get(old_entry.entity_id)
        assert migrated is not None, f"entity_id {old_entry.entity_id!r} lost after migration"
        assert migrated.unique_id == expected_new_uid


# --- #52: p_grid_out renamed to grid_power (signed net, not export-only) ---


async def test_grid_power_old_uid_gone(hass, setup_integration):
    """Old p_grid_out unique_id must be absent after migration."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_p_grid_out") is None, (
        "Old unique_id 'SA1234G123_p_grid_out' still registered — migration didn't run"
    )


async def test_unique_id_migration_repoints_grid_power(hass, mock_client, mock_config_entry):
    """Pre-rename entities under the old p_grid_out unique_id are re-pointed in place."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_p_grid_out", config_entry=mock_config_entry
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    migrated = registry.async_get(old.entity_id)
    assert migrated is not None, f"entity_id {old.entity_id!r} lost after migration"
    assert migrated.unique_id == "SA1234G123_grid_power"


async def test_unique_id_migration_also_renames_grid_export_power_entity_id(
    hass, mock_client, mock_config_entry
):
    """Migration of p_grid_out → grid_power also renames the entity_id when it
    still carries the old '_grid_export_power' slug, so dashboard references to
    '…_grid_power' resolve to the existing entity rather than a missing one."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_p_grid_out", config_entry=mock_config_entry
    )
    # Simulate the entity_id that HA would have assigned when the sensor was
    # first registered under the old "Grid Export Power" name.
    old_entity_id = "sensor.givenergy_inverter_sa1234g123_grid_export_power"
    registry.async_update_entity(old.entity_id, new_entity_id=old_entity_id)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Old entity_id must be gone and re-issued under the new slug.
    assert registry.async_get(old_entity_id) is None
    new_entity_id = "sensor.givenergy_inverter_sa1234g123_grid_power"
    migrated = registry.async_get(new_entity_id)
    assert migrated is not None, f"entity_id {new_entity_id!r} not found after migration"
    assert migrated.unique_id == "SA1234G123_grid_power"
    assert migrated.entity_id == new_entity_id


async def test_entity_id_rename_fires_when_unique_id_already_migrated(
    hass, mock_client, mock_config_entry
):
    """Idempotency: on an install where an earlier release already moved the
    unique_id to grid_power but left the entity_id at '_grid_export_power', the
    entity_id rename must still fire — it can't be gated on the (now-new) unique_id.
    Regression for the rc1→rc2 path where the rename silently no-op'd."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # unique_id ALREADY at the new value, entity_id still the stale slug.
    ent = registry.async_get_or_create(
        "sensor", DOMAIN, "SA1234G123_grid_power", config_entry=mock_config_entry
    )
    old_entity_id = "sensor.givenergy_inverter_sa1234g123_grid_export_power"
    registry.async_update_entity(ent.entity_id, new_entity_id=old_entity_id)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old_entity_id) is None, "stale entity_id not renamed"
    new_entity_id = "sensor.givenergy_inverter_sa1234g123_grid_power"
    migrated = registry.async_get(new_entity_id)
    assert migrated is not None, f"entity_id {new_entity_id!r} not found after migration"
    assert migrated.unique_id == "SA1234G123_grid_power"


# ---------------------------------------------------------------------------
# All-in-One per-module battery devices (#192)
# ---------------------------------------------------------------------------


def _mock_aio_module(serial: str, address: int, *, valid: bool = True) -> MagicMock:
    """A stand-in AioBatteryModule: 24 cell voltages, 12 populated temps."""
    module = MagicMock()
    module.serial_number = serial
    module.module_address = address
    module.is_valid.return_value = valid
    for i in range(1, 25):
        setattr(module, f"v_cell_{i:02d}", 3.30 + i * 0.001)
    for i in range(1, 13):
        setattr(module, f"t_cell_{i:02d}", 20.0 + i * 0.1)
    for i in range(13, 25):
        setattr(module, f"t_cell_{i:02d}", 0.0)  # unpopulated on real hardware
    return module


def _setup_aio_plant(mock_client, modules: list[MagicMock]) -> None:
    """Reshape the mock plant into an All-in-One exposing `modules`."""
    from givenergy_modbus.model.inverter import Model
    from givenergy_modbus.model.plant import PlantCapabilities

    addresses = [m.module_address for m in modules]
    mock_client.plant.aio_battery_modules = modules
    mock_client.plant.capabilities = PlantCapabilities(
        device_type=Model.ALL_IN_ONE,
        inverter_address=0x31,
        meter_addresses=[],
        lv_battery_addresses=[],
        bcu_stacks=[],
        aio_battery_module_addresses=addresses,
    )


@pytest.fixture
async def aio_setup(hass, mock_client, mock_config_entry):
    """Set up the integration as an All-in-One with two valid battery modules."""
    _setup_aio_plant(
        mock_client,
        [_mock_aio_module("HX2414G831", 0x50), _mock_aio_module("HX2414G832", 0x51)],
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_aio_modules_create_one_device_each(hass, aio_setup):
    """Each module is its own device, keyed by serial and linked to the inverter."""
    registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)
    for serial in ("HX2414G831", "HX2414G832"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_v_cell_01")
        assert entity_id is not None, f"module {serial} has no cell sensor"
        device = dev_registry.async_get(registry.async_get(entity_id).device_id)
        assert device is not None
        assert (DOMAIN, serial) in device.identifiers
        assert device.serial_number == serial
        assert device.model == "AIO Battery Module"
        # Parented to the AIO inverter device specifically, not just any parent.
        inverter_device = dev_registry.async_get_device(identifiers={(DOMAIN, "SA1234G123")})
        assert inverter_device is not None
        assert device.via_device_id == inverter_device.id


async def test_aio_module_all_24_voltage_cells(hass, aio_setup):
    registry = er.async_get(hass)
    for i in range(1, 25):
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"HX2414G831_v_cell_{i:02d}") is not None
        ), f"v_cell_{i:02d} missing"
    state = hass.states.get(_entity_id(hass, "sensor", "HX2414G831_v_cell_01"))
    assert float(state.state) == 3.301  # 3.30 + 1*0.001
    assert state.attributes["unit_of_measurement"] == "V"


async def test_aio_module_entity_tracks_serial_not_list_index(hass, aio_setup):
    """If a module drops out and the list reindexes, each entity must keep
    reporting its own module — never cross-wire to a neighbour, and go
    unavailable when its module is absent (#192 review)."""
    coordinator = hass.data[DOMAIN][aio_setup.entry_id]
    # The first module (HX2414G831) drops out of the poll; only the second
    # (HX2414G832) remains, now at list index 0 with a distinctive cell value.
    surviving = _mock_aio_module("HX2414G832", 0x51)
    surviving.v_cell_01 = 3.500
    coordinator.data.aio_battery_modules = [surviving]
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()

    # The survivor reports its own value under its own serial — not the dropped
    # module's (which would be the bug if entities indexed by position).
    survivor_state = hass.states.get(_entity_id(hass, "sensor", "HX2414G832_v_cell_01"))
    assert float(survivor_state.state) == 3.500
    # The dropped module's entity goes unavailable rather than borrowing index 0.
    dropped_state = hass.states.get(_entity_id(hass, "sensor", "HX2414G831_v_cell_01"))
    assert dropped_state.state == "unavailable"


async def test_aio_module_exposes_only_first_twelve_temps(hass, aio_setup):
    """Cells 1-12 get temperature sensors; 13-24 (zero on hardware) are omitted."""
    registry = er.async_get(hass)
    for i in range(1, 13):
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"HX2414G831_t_cell_{i:02d}") is not None
        ), f"t_cell_{i:02d} should exist"
    for i in range(13, 25):
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"HX2414G831_t_cell_{i:02d}") is None
        ), f"t_cell_{i:02d} should not be exposed"


async def test_aio_module_with_invalid_serial_is_skipped(hass, mock_client, mock_config_entry):
    """A module with a blank/invalid serial can't anchor a device, so it's skipped."""
    _setup_aio_plant(
        mock_client,
        [_mock_aio_module("HX2414G831", 0x50), _mock_aio_module("", 0x51, valid=False)],
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    dev_registry = dr.async_get(hass)
    modules = [
        d
        for d in dr.async_entries_for_config_entry(dev_registry, mock_config_entry.entry_id)
        if d.model == "AIO Battery Module"
    ]
    assert len(modules) == 1
    assert modules[0].serial_number == "HX2414G831"


async def test_non_aio_plant_creates_no_module_entities(hass, setup_integration):
    """The default (non-AIO) fixture must not create any per-module entities."""
    dev_registry = dr.async_get(hass)
    modules = [
        d
        for d in dr.async_entries_for_config_entry(dev_registry, setup_integration.entry_id)
        if d.model == "AIO Battery Module"
    ]
    assert modules == []


def test_aio_module_sensor_descriptions_cover_expected_cells():
    """Exactly v_cell_01..24 and t_cell_01..12 — exact sets, so a shifted or
    missing index is caught, not just a matching count."""
    keys = [d.key for d in AIO_MODULE_SENSORS]
    assert len(keys) == len(set(keys))
    assert {k for k in keys if k.startswith("v_cell_")} == {f"v_cell_{i:02d}" for i in range(1, 25)}
    assert {k for k in keys if k.startswith("t_cell_")} == {f"t_cell_{i:02d}" for i in range(1, 13)}


# ---------------------------------------------------------------------------
# Split grid import/export power sensors (Energy Dashboard "Two sensors")
# ---------------------------------------------------------------------------


def test_grid_split_power_helpers_resolve_both_directions():
    """Each helper is the always-positive magnitude of its own direction, 0 in
    the other, and None before the first poll."""
    from custom_components.givenergy_local.sensor import _grid_export_power, _grid_import_power

    exporting = MagicMock(p_grid_out=1500)
    importing = MagicMock(p_grid_out=-800)
    unpolled = MagicMock(p_grid_out=None)

    assert _grid_export_power(exporting) == 1500
    assert _grid_import_power(exporting) == 0
    assert _grid_import_power(importing) == 800
    assert _grid_export_power(importing) == 0
    assert _grid_export_power(unpolled) is None
    assert _grid_import_power(unpolled) is None


async def test_grid_split_power_sensors_created(hass, setup_integration):
    """Both split power sensors are created; mock p_grid_out=-800 (importing)."""
    imp = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_grid_power_import"))
    exp = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_grid_power_export"))
    assert float(imp.state) == 800
    assert float(exp.state) == 0
    assert imp.attributes["unit_of_measurement"] == "W"
    assert imp.attributes["device_class"] == "power"


async def test_grid_power_hidden_by_default(hass, setup_integration):
    """The signed bidirectional grid_power stays recorded (the bundled flow card
    keys off it) but is hidden by default in favour of the split sensors."""
    registry = er.async_get(hass)
    entry = registry.async_get(_entity_id(hass, "sensor", "SA1234G123_grid_power"))
    assert entry.hidden_by is not None


# ---------------------------------------------------------------------------
# Stale IR bank → unavailable (#152)
# ---------------------------------------------------------------------------


def _register_shape(registers):
    """(reg_type, index) view of a Register tuple, for order-pinned assertions."""
    return [(r.reg_type, r.index) for r in registers]


def test_source_ir_registers_resolves_via_model_lut():
    """Register-backed keys map to their IR registers through the model's public
    registers_of() accessor (givenergy-modbus 2.3.0, #248); computed fields (not
    in the LUT) and HR-backed config resolve to nothing — HR banks legitimately
    age between full refreshes, so they're no signal."""
    from givenergy_modbus.model.inverter import SinglePhaseInverter
    from givenergy_modbus.model.inverter_threephase import ThreePhaseInverter

    from custom_components.givenergy_local.sensor import _source_ir_registers

    assert _register_shape(_source_ir_registers(SinglePhaseInverter, "e_grid_out_day")) == [
        ("IR", 25)
    ]
    assert _register_shape(_source_ir_registers(ThreePhaseInverter, "e_load_today")) == [
        ("IR", 1396),
        ("IR", 1397),
    ]
    assert _source_ir_registers(SinglePhaseInverter, "e_consumption_today") == ()
    assert _source_ir_registers(SinglePhaseInverter, "charge_target_soc") == ()
    # Mock model classes (as used across these tests) resolve to nothing.
    assert _source_ir_registers(MagicMock, "e_grid_out_day") == ()


def test_renamed_direct_register_sensors_declare_their_source_field():
    """Descriptors whose entity key differs from the model field they read must
    carry source_field, or they'd silently fall outside the stale-bank protection
    (Codex review on #158): grid_power* all read p_grid_out, work_time_total
    reads work_time_total_hours, and the consumption sensor's three-phase path
    reads the native e_load_today (#156 follow-up). Genuinely computed/derived
    fields (multi-register sums, per-model aliases like the battery
    charge/discharge canonical names) stay deliberately untracked."""
    from givenergy_modbus.model.inverter import SinglePhaseInverter
    from givenergy_modbus.model.inverter_threephase import ThreePhaseInverter

    from custom_components.givenergy_local.sensor import _source_ir_registers

    expected = {
        "grid_power": "p_grid_out",
        "grid_power_import": "p_grid_out",
        "grid_power_export": "p_grid_out",
        "work_time_total": "work_time_total_hours",
        "e_consumption_today": "e_load_today",
    }
    declared = {d.key: d.source_field for d in INVERTER_SENSORS if d.source_field is not None}
    assert declared == expected
    # Every declared override must resolve to IR registers on at least one
    # model — a typo'd source_field would silently disable the protection it
    # exists to provide.
    for key, source in expected.items():
        assert _source_ir_registers(SinglePhaseInverter, source) or _source_ir_registers(
            ThreePhaseInverter, source
        ), f"{key}: source_field {source!r} resolves to no IR registers on any model"
    # The consumption source is per-model by construction: on single-phase the
    # value is the derived field (untracked — e_load_today isn't in that LUT),
    # on three-phase it's the native register the value_fn falls back to.
    assert _source_ir_registers(SinglePhaseInverter, "e_load_today") == ()
    assert _register_shape(_source_ir_registers(ThreePhaseInverter, "e_load_today")) == [
        ("IR", 1396),
        ("IR", 1397),
    ]


def test_sensor_unavailable_when_backing_ir_block_stale(mock_plant):
    """A backing IR bank that stopped committing past the ceiling drops the
    sensor to unavailable; a never-committed bank is no signal (the Pattern B
    shape is 'committed real data, then stopped' — #152). Freshness comes from
    the library's Plant.register_age() (2.3.0, #248)."""
    from datetime import timedelta

    from givenergy_modbus.model.register import IR

    from custom_components.givenergy_local.sensor import GivEnergyInverterSensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    entity = GivEnergyInverterSensor(coordinator, _inverter_desc("e_grid_out_day"))
    # The conftest inverter is a MagicMock with no register LUT, so inject the
    # source registers the resolver would derive from the real model.
    entity._source_ir_registers = (IR(25),)
    mock_plant.register_age = MagicMock()

    # Fresh bank: available.
    mock_plant.register_age.return_value = 35.0
    assert entity.available is True
    # Asked the plant about the right device and register.
    (addr, reg) = mock_plant.register_age.call_args.args
    assert addr == 0x32  # conftest capabilities.inverter_address
    assert (reg.reg_type, reg.index) == ("IR", 25)
    # Bank stopped committing 10 minutes ago (ceiling at 30 s interval = 300 s).
    mock_plant.register_age.return_value = 600.0
    assert entity.available is False
    # Never committed: stays available (its value reads None/unknown anyway).
    mock_plant.register_age.return_value = None
    assert entity.available is True
    # Coordinator-level failure still wins regardless of bank ages.
    mock_plant.register_age.return_value = 35.0
    coordinator.last_update_success = False
    assert entity.available is False


def test_sensor_without_ir_source_keeps_default_availability(mock_plant):
    """Computed / HR-backed / mock-model sensors never consult bank ages."""
    from datetime import timedelta

    from custom_components.givenergy_local.sensor import GivEnergyInverterSensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    entity = GivEnergyInverterSensor(coordinator, _inverter_desc("e_consumption_today"))

    assert entity._source_ir_registers == ()
    # Freshness must not even be consulted.
    mock_plant.register_age = MagicMock()
    assert entity.available is True
    mock_plant.register_age.assert_not_called()


def _battery_desc(key: str):
    return next(d for d in BATTERY_SENSORS if d.key == key)


def test_battery_sensor_unavailable_when_backing_ir_block_stale(mock_plant):
    """A battery's IR bank that stopped committing past the ceiling drops the
    pack's sensors to unavailable — the path that catches the library keeping
    last-good on a sub-bus splice (#176/#152). Freshness via Plant.register_age()."""
    from datetime import timedelta

    from givenergy_modbus.model.register import IR

    from custom_components.givenergy_local.sensor import GivEnergyBatterySensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    entity = GivEnergyBatterySensor(coordinator, _battery_desc("soc"), 0)
    # The conftest battery is a MagicMock with no register LUT, so inject the
    # source registers the resolver would derive from the real Battery model.
    entity._source_ir_registers = (IR(64),)
    mock_plant.register_age = MagicMock()

    # Fresh bank: available, and queried against the battery's device address.
    mock_plant.register_age.return_value = 35.0
    assert entity.available is True
    (addr, reg) = mock_plant.register_age.call_args.args
    assert addr == 0x32  # conftest capabilities.lv_battery_addresses[0]
    assert (reg.reg_type, reg.index) == ("IR", 64)
    # Bank stopped committing 10 minutes ago (ceiling at 30 s interval = 300 s).
    mock_plant.register_age.return_value = 600.0
    assert entity.available is False
    # Never committed: stays available (its value reads None/unknown anyway).
    mock_plant.register_age.return_value = None
    assert entity.available is True
    # Coordinator-level failure still wins regardless of bank ages.
    mock_plant.register_age.return_value = 35.0
    coordinator.last_update_success = False
    assert entity.available is False


def test_battery_sensor_without_ir_source_keeps_default_availability(mock_plant):
    """A battery sensor whose key resolves to no IR registers never consults ages."""
    from datetime import timedelta

    from custom_components.givenergy_local.sensor import GivEnergyBatterySensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    entity = GivEnergyBatterySensor(coordinator, _battery_desc("soc"), 0)

    assert entity._source_ir_registers == ()  # mock battery model has no LUT
    mock_plant.register_age = MagicMock()
    assert entity.available is True
    mock_plant.register_age.assert_not_called()


def test_battery_sensor_available_when_index_out_of_range(mock_plant):
    """A pack index beyond the known battery addresses keeps default availability
    (bounds guard) rather than indexing past the capabilities list."""
    from datetime import timedelta

    from givenergy_modbus.model.register import IR

    from custom_components.givenergy_local.sensor import GivEnergyBatterySensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    entity = GivEnergyBatterySensor(coordinator, _battery_desc("soc"), 0)
    entity._battery_index = 5  # beyond capabilities.lv_battery_addresses ([0x32])
    entity._source_ir_registers = (IR(64),)
    mock_plant.register_age = MagicMock()

    assert entity.available is True
    mock_plant.register_age.assert_not_called()


# --- #142: monotonic clamp must not accept transient dips as counter resets ---


def _monotonic_entity(mock_plant):
    from datetime import timedelta

    from custom_components.givenergy_local.sensor import GivEnergyInverterSensor

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator.data = mock_plant
    return GivEnergyInverterSensor(coordinator, _inverter_desc("e_consumption_today"))


def _read(entity, mock_plant, value):
    mock_plant.inverter.e_consumption_today = value
    return entity.native_value


def test_monotonic_clamp_holds_through_transient_dip(mock_plant, freezer):
    """A transient register-skew dip — any size, any sign — must be held at the
    previous max, not adopted as a 'reset' (#142: a one-poll zeroed PV read sank
    the derived value to -2.2 / 1.4 and the recorder double-counted the
    recovery as ~20 kWh of new consumption)."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    # The #142 excursion: negative dip held, never exposed.
    assert _read(entity, mock_plant, -2.2) == 14.3
    # A sag to a still-large value is no more plausible a reset.
    assert _read(entity, mock_plant, 12.1) == 14.3
    # Recovery is a plain increase — no fake reset, no double-count.
    assert _read(entity, mock_plant, 14.5) == 14.5


def test_monotonic_never_exposes_negative_baseline(mock_plant, freezer):
    """A negative first/new-day reading floors the baseline at zero."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, -2.2) == 0.0
    assert _read(entity, mock_plant, 0.4) == 0.4

    # Negative skew spanning the day boundary: the new-day baseline is floored.
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, -1.0) == 0.0


def test_monotonic_zero_baseline_small_dip(mock_plant, freezer):
    """Falsy-zero regression: with a 0.0 baseline, a sub-threshold dip used to
    slip through `self._monotonic_max or value` and expose a negative."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 0.0) == 0.0
    assert _read(entity, mock_plant, -0.3) == 0.0
    assert _read(entity, mock_plant, 0.2) == 0.2


def test_monotonic_midnight_reset_passes(mock_plant, freezer):
    """The genuine midnight reset still passes through as a real decrease."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 21.4) == 21.4
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, 0.1) == 0.1


def test_monotonic_reset_accepted_after_poll_gap(mock_plant, freezer):
    """The post-reset plausibility ceiling scales with time since the previous
    reading: after a polling outage spanning the counter reset, or on a long
    scan interval under heavy load, the first reading has legitimately
    accumulated more than the 0.5 kWh floor and must still be accepted."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    # HA's date flips; the lagging counter still reads yesterday's total.
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, 14.3) == 14.3
    # Two hours of failed polls; the counter reset during the gap and has
    # since accumulated 1.8 kWh — far over the floor, well under 15 kW × 2 h.
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 1.8) == 1.8
    assert _read(entity, mock_plant, 2.0) == 2.0


def test_monotonic_reset_accepted_on_long_scan_interval(mock_plant, freezer):
    """A 5-minute scan interval at high load: the first post-reset reading can
    exceed the 0.5 kWh floor (e.g. 0.9 kWh at ~11 kW); the elapsed-scaled
    ceiling (15 kW × 300 s = 1.25 kWh) accepts it."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(300)
    assert _read(entity, mock_plant, 0.9) == 0.9


def test_monotonic_daytime_gap_does_not_widen_acceptance(mock_plant, freezer):
    """An ordinary daytime polling gap must not widen the reset band: no reset
    is owed (the day's reset was already observed), so a still-large sag on
    the first post-gap poll stays clamped (review on #163)."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    # Two-hour outage, then a skew sag to a still-large value: without the
    # reset-pending gate, the elapsed-scaled ceiling (30 kWh) would accept
    # 12.1 as a "reset" and double-count the recovery.
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 12.1) == 14.3
    assert _read(entity, mock_plant, 14.4) == 14.4


def test_monotonic_pending_reset_consumed_by_acceptance(mock_plant, freezer):
    """Once the owed reset has been accepted, later same-day gaps revert to
    the tight floor — a subsequent dip is skew, not another reset."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 1.8) == 1.8  # owed reset accepted
    assert _read(entity, mock_plant, 2.0) == 2.0
    # Another gap the same day: the band is back at the floor.
    freezer.tick(3600)
    assert _read(entity, mock_plant, 0.9) == 2.0


def test_monotonic_pending_survives_negative_boundary_reading(mock_plant, freezer):
    """A transient negative reading exactly at the date flip must not decide
    the owed-reset question: the carry-over reappears on the next poll, and
    the genuine reset after a later gap must still be admitted (review on
    #163)."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    # The boundary poll itself is register skew gone negative.
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, -1.0) == 0.0
    # Next poll: yesterday's still-unreset total reappears — reset still owed.
    assert _read(entity, mock_plant, 14.3) == 14.3
    # The owed reset lands after a two-hour gap, above the fixed floor.
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 1.8) == 1.8


def test_monotonic_deferred_decision_skips_ambiguous_sag(mock_plant, freezer):
    """While the owed-reset question is deferred past a skewed boundary
    reading, an ambiguous still-large sag (neither carry-over nor a plausible
    reset) must not settle it — nor be exposed. A later plausible poll makes
    the call (review on #163)."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, -1.0) == 0.0  # boundary skew: undecided
    assert _read(entity, mock_plant, 12.1) == 0.0  # ambiguous sag: held, still undecided
    assert _read(entity, mock_plant, 14.3) == 14.3  # carry-over: reset owed
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 1.8) == 1.8  # owed reset admitted


def test_monotonic_deferred_decision_settles_on_plausible_reset(mock_plant, freezer):
    """The other arm of the deferred call: after boundary skew, a reading in
    the (elapsed-scaled) reset band means the reset happened during the skew
    window — decided as seen, counting resumes."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, -1.0) == 0.0
    assert _read(entity, mock_plant, 0.2) == 0.2  # plausible post-reset: decided
    # The band is now back at the floor: a later still-large sag stays held.
    freezer.tick(2 * 3600)
    assert _read(entity, mock_plant, 5.0) == 5.0  # normal accumulation
    assert _read(entity, mock_plant, 3.0) == 5.0  # still-large sag clamped


def test_monotonic_negative_still_rejected_after_poll_gap(mock_plant, freezer):
    """A widened ceiling never admits a negative excursion."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 14.3) == 14.3
    freezer.tick(3600)
    assert _read(entity, mock_plant, -2.2) == 14.3


def test_monotonic_clock_lag_reset_after_date_flip(mock_plant, freezer):
    """An inverter clock lagging HA's midnight: the pre-reset value carries over
    as the new-day baseline, and the real reset lands a poll later via the
    magnitude branch — gated on a plausible post-reset value."""
    freezer.move_to("2026-06-12 12:00:00+00:00")
    entity = _monotonic_entity(mock_plant)

    assert _read(entity, mock_plant, 21.4) == 21.4
    # HA's date flips first; the counter hasn't reset yet.
    freezer.tick(24 * 3600)
    assert _read(entity, mock_plant, 21.4) == 21.4
    # The actual reset arrives on the next poll.
    assert _read(entity, mock_plant, 0.05) == 0.05
    # Counting resumes from the new baseline.
    assert _read(entity, mock_plant, 0.3) == 0.3


async def test_monotonic_restore_floors_negative(hass, mock_plant):
    """A persisted negative state (recorded by pre-fix versions) must not
    re-seed a negative baseline after restart."""
    from unittest.mock import AsyncMock

    from homeassistant.core import State

    entity = _monotonic_entity(mock_plant)
    entity.hass = hass
    entity.entity_id = "sensor.test_house_consumption_today"
    entity.async_get_last_state = AsyncMock(return_value=State(entity.entity_id, "-2.2"))
    await entity.async_added_to_hass()

    assert entity._monotonic_max == 0.0
    assert _read(entity, mock_plant, -0.3) == 0.0


async def test_monotonic_restore_seeds_same_day_max(hass, mock_plant):
    """A same-day persisted value seeds the intra-day max, so a transient dip
    on the first post-restart poll is held rather than adopted."""
    from unittest.mock import AsyncMock

    from homeassistant.core import State

    entity = _monotonic_entity(mock_plant)
    entity.hass = hass
    entity.entity_id = "sensor.test_house_consumption_today"
    entity.async_get_last_state = AsyncMock(return_value=State(entity.entity_id, "13.7"))
    await entity.async_added_to_hass()

    assert entity._monotonic_max == 13.7
    # A >threshold drop to a still-large value is rejected by the reset gate.
    assert _read(entity, mock_plant, 12.1) == 13.7
    assert _read(entity, mock_plant, 13.8) == 13.8


async def test_monotonic_restore_prior_day_carry_over_allows_late_reset(hass, mock_plant, freezer):
    """A restart spanning midnight: the persisted state is from yesterday, and
    the first reading of the new day still matches it (inverter clock lag).
    The owed reset arriving on a later poll must be admitted."""
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    freezer.move_to("2026-06-13 00:02:00+00:00")
    yesterday = dt_util.utcnow() - timedelta(days=1)
    entity = _monotonic_entity(mock_plant)
    entity.hass = hass
    entity.entity_id = "sensor.test_house_consumption_today"
    entity.async_get_last_state = AsyncMock(
        return_value=State(entity.entity_id, "14.3", last_updated=yesterday)
    )
    await entity.async_added_to_hass()
    assert entity._monotonic_max is None  # prior-day state is not a baseline

    # First reading still carries yesterday's total — reset owed.
    assert _read(entity, mock_plant, 14.3) == 14.3
    # The real reset lands two polls later, after some accumulation.
    freezer.tick(120)
    assert _read(entity, mock_plant, 0.3) == 0.3


async def test_monotonic_restore_skips_unusable_states(hass, mock_plant):
    """Non-numeric or prior-day persisted states must not seed the baseline."""
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    entity = _monotonic_entity(mock_plant)
    entity.hass = hass
    entity.entity_id = "sensor.test_house_consumption_today"
    entity.async_get_last_state = AsyncMock(return_value=State(entity.entity_id, "unavailable"))
    await entity.async_added_to_hass()
    assert entity._monotonic_max is None

    yesterday = dt_util.utcnow() - timedelta(days=1)
    entity = _monotonic_entity(mock_plant)
    entity.hass = hass
    entity.entity_id = "sensor.test_house_consumption_today"
    entity.async_get_last_state = AsyncMock(
        return_value=State(entity.entity_id, "13.7", last_updated=yesterday)
    )
    await entity.async_added_to_hass()
    assert entity._monotonic_max is None
