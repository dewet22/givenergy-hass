"""Sensor platform for givenergy."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature, UnitOfApparentPower, UnitOfElectricCurrent, UnitOfTime, EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, Icon, LOGGER
from .coordinator import GivEnergyCoordinator
from .entity import InverterEntity, BatteryEntity

INVERTER_ENTITIES = (
    # Energy - inverter
    SensorEntityDescription(
        key="e_inverter_in_day",
        name="Inverter energy in today",
        icon=Icon.INVERTER,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_inverter_in_total",
        name="Total inverter energy in",
        icon=Icon.INVERTER,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_inverter_out_day",
        name="Inverter energy out today",
        icon=Icon.INVERTER,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_inverter_out_total",
        name="Total inverter energy out",
        icon=Icon.INVERTER,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),

    # Energy - grid
    SensorEntityDescription(
        key="e_grid_in_day",
        name="Grid energy import today",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_grid_in_total",
        name="Total grid energy import",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_grid_out_day",
        name="Grid energy export today",
        icon=Icon.GRID_EXPORT,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_grid_out_total",
        name="Total grid energy export",
        icon=Icon.GRID_EXPORT,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),

    # Energy - PV
    SensorEntityDescription(
        key="e_pv1_day",
        name="Solar energy produced today (chain 1)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_pv2_day",
        name="Solar energy produced today (chain 2)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_pv_total",
        name="Total solar energy produced",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_solar_diverter",
        name="Solar diverted energy",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),

    # Energy - inverter battery
    SensorEntityDescription(
        key="e_battery_charge_day",
        name="Battery charge energy today",
        icon=Icon.BATTERY_PLUS,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_battery_charge_total",
        name="Total battery charge energy",
        icon=Icon.BATTERY_PLUS,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_battery_discharge_day",
        name="Battery discharge energy today",
        icon=Icon.BATTERY_MINUS,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_battery_discharge_total",
        name="Total battery discharge energy",
        icon=Icon.BATTERY_MINUS,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="e_battery_throughput_total",
        name="Total battery throughput energy",
        icon=Icon.BATTERY,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),

    # Power
    SensorEntityDescription(
        key="p_inverter_out",
        name="Inverter power out",
        icon=Icon.INVERTER,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_grid_out",
        name="Grid export power",
        icon=Icon.GRID_EXPORT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_grid_apparent",
        name="Grid export apparent power",
        icon=Icon.GRID_EXPORT,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
    ),
    SensorEntityDescription(
        key="p_load_demand",
        name="Load power",
        icon=Icon.LOAD,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_eps_backup",
        name="EPS load power",
        icon=Icon.EPS,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_pv1",
        name="Solar array power (chain 1)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_pv2",
        name="Solar array power (chain 2)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="p_battery",
        name="Battery power out",
        icon=Icon.BATTERY,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),

    # Voltages
    SensorEntityDescription(
        key="v_ac1",
        name="Grid voltage",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),
    SensorEntityDescription(
        key="v_pv1",
        name="Solar array voltage (chain 1)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),
    SensorEntityDescription(
        key="v_pv2",
        name="Solar array voltage (chain 2)",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),
    SensorEntityDescription(
        key="v_battery",
        name="Battery voltage",
        icon=Icon.BATTERY,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),
    SensorEntityDescription(
        key="v_eps_backup",
        name="EPS voltage",
        icon=Icon.EPS,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),

    # Current
    SensorEntityDescription(
        key="i_ac1",
        name="Grid current",  # inverter internal consumption?
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    SensorEntityDescription(
        key="i_grid_port",
        name="Grid port current",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    SensorEntityDescription(
        key="i_pv1",
        name="Solar array current (chain 1)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    SensorEntityDescription(
        key="i_pv2",
        name="Solar array current (chain 2)",
        icon=Icon.SOLAR,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    SensorEntityDescription(
        key="i_battery",
        name="Battery current out",
        icon=Icon.BATTERY,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),

    # Frequencies
    SensorEntityDescription(
        key="f_ac1",
        name="Grid frequency",
        icon=Icon.GRID_IMPORT,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
    ),
    SensorEntityDescription(
        key="f_eps_backup",
        name="EPS frequency",
        icon=Icon.EPS,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
    ),

    # Temperatures
    SensorEntityDescription(
        key="temp_inverter_heatsink",
        name="Heatsink temperature",
        icon=Icon.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    SensorEntityDescription(
        key="temp_charger",
        name="Charger temperature",
        icon=Icon.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    SensorEntityDescription(
        key="temp_battery",
        name="Battery temperature",
        icon=Icon.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),

    SensorEntityDescription(
        key="battery_percent",
        name="Battery SOC",
        icon=Icon.BATTERY,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),

    # all the rest, mostly diagnostic
    SensorEntityDescription(
        key="inverter_status",
        name="Status",
        device_class=SensorDeviceClass.ENUM,
        translation_key="inverter_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="charge_status",
        name="Charge status",
        device_class=SensorDeviceClass.ENUM,
        translation_key="charge_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="work_time_total",
        name="Total work time",
        icon="mdi:timer-play",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="system_mode",
        name="System mode",
        device_class=SensorDeviceClass.ENUM,
        translation_key="system_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="device_type_code",
        name="Device type code",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),


)

BATTERY_ENTITIES = (
    SensorEntityDescription(
        key="design_capacity",
        name="Design charge capacity",
        icon=Icon.BATTERY,
        # device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement='Ah',
    ),
    SensorEntityDescription(
        key="full_capacity",
        name="Actual charge capacity",
        icon=Icon.BATTERY,
        # device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement='Ah',
    ),
    SensorEntityDescription(
        key="remaining_capacity",
        name="Remaining charge",
        icon=Icon.BATTERY,
        # device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement='Ah',
    ),
    SensorEntityDescription(
        key="num_cycles",
        name="Charge cycles",
        icon=Icon.BATTERY_CYCLES,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="soc",
        name="State of charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="v_battery_out",
        name="Output voltage",
        icon=Icon.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),

)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the sensor platform."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(InverterSensor(coordinator=coordinator, entity_description=e) for e in INVERTER_ENTITIES)
    for i in range(coordinator.plant.number_batteries):
        async_add_entities(BatterySensor(coordinator=coordinator, entity_description=e, battery_id=i) for e in BATTERY_ENTITIES)


class InverterSensor(InverterEntity, SensorEntity):
    """GivEnergy inverter sensor class."""

    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: SensorEntityDescription):
        """Initialize the sensor class."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.coordinator.data.inverter_serial_number}_{entity_description.key}"
        self._attr_name = f'Inverter {self.coordinator.data.inverter_serial_number} {entity_description.name}'
        self.entity_description = entity_description

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        return getattr(self.coordinator.inverter, self.entity_description.key)
class BatterySensor(BatteryEntity, SensorEntity):
    """GivEnergy battery entity class."""

    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: SensorEntityDescription, battery_id: int):
        """Initialize the sensor class."""
        super().__init__(coordinator, battery_id)
        self.battery = self.coordinator.data.batteries[battery_id]
        self._attr_unique_id = f"{self.battery.battery_serial_number}_{entity_description.key}"
        self._attr_name = f'Battery {self.battery.battery_serial_number} {entity_description.name}'
        self.entity_description = entity_description

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        return getattr(self.battery, self.entity_description.key)
