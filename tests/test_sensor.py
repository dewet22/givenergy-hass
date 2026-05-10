"""Tests for the GivEnergy Local sensor platform."""
import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN
from custom_components.givenergy_local.sensor import BATTERY_SENSORS, INVERTER_SENSORS


def _entity_id(hass, platform: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"No entity found for unique_id={unique_id!r}"
    return entity_id


async def test_expected_sensor_count(hass, setup_integration):
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_integration.entry_id)
    sensors = [e for e in entries if e.domain == "sensor"]
    # 1 battery → len(INVERTER_SENSORS) + len(BATTERY_SENSORS)
    assert len(sensors) == len(INVERTER_SENSORS) + len(BATTERY_SENSORS)


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
