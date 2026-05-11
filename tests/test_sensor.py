"""Tests for the GivEnergy Local sensor platform."""

from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN
from custom_components.givenergy_local.sensor import (
    BATTERY_SENSORS,
    COORDINATOR_SENSORS,
    INVERTER_SENSORS,
)


def _entity_id(hass, platform: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"No entity found for unique_id={unique_id!r}"
    return entity_id


async def test_expected_sensor_count(hass, setup_integration):
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_integration.entry_id)
    sensors = [e for e in entries if e.domain == "sensor"]
    # 1 battery → inverter sensors + battery sensors + coordinator diagnostics
    expected = len(INVERTER_SENSORS) + len(BATTERY_SENSORS) + len(COORDINATOR_SENSORS)
    assert len(sensors) == expected


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
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_p_grid_out"))
    assert float(state.state) == -800


async def test_work_time_converted_to_hours(hass, setup_integration):
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_work_time_total"))
    # 36_000_000 seconds / 3600 = 10000.0 hours
    assert float(state.state) == 10000.0


async def test_inverter_device_info(hass, setup_integration):
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id("sensor", DOMAIN, "SA1234G123_p_pv")
    entity_entry = registry.async_get(entry)
    from homeassistant.helpers import device_registry as dr

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
    from homeassistant.helpers import device_registry as dr

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


async def test_cell_voltages_attached_to_battery_device(hass, setup_integration):
    """Per-cell sensors live on the battery device, not the inverter device."""
    from homeassistant.helpers import device_registry as dr

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
        "charge_status": "1",
        "battery_pause_mode": "0",
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
    """meter_type and battery_type enums should surface as title-cased names, not ints."""
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_battery_type"))
    assert state.state == "Lithium"
    state = hass.states.get(_entity_id(hass, "sensor", "SA1234G123_meter_type"))
    assert state.state == "Ct Or Em418"


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
    mock_client.refresh_plant.side_effect = TimeoutError()
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
