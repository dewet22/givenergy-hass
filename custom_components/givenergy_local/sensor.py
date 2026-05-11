from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from givenergy_modbus.model.battery import Battery
from givenergy_modbus.model.inverter import Inverter
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class GivEnergyInverterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Inverter], Any] = field(default=lambda _: None)


@dataclass(frozen=True, kw_only=True)
class GivEnergyBatterySensorDescription(SensorEntityDescription):
    value_fn: Callable[[Battery], Any] = field(default=lambda _: None)


@dataclass(frozen=True, kw_only=True)
class GivEnergyCoordinatorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GivEnergyUpdateCoordinator], Any] = field(default=lambda _: None)


INVERTER_SENSORS: tuple[GivEnergyInverterSensorDescription, ...] = (
    # --- Status ---
    GivEnergyInverterSensorDescription(
        key="status",
        name="Status",
        value_fn=lambda inv: inv.status.name.replace("_", " ").title(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="fault_code",
        name="Fault Code",
        value_fn=lambda inv: inv.fault_code,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Solar / PV ---
    GivEnergyInverterSensorDescription(
        key="p_pv",
        name="PV Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_pv(),
    ),
    GivEnergyInverterSensorDescription(
        key="p_pv1",
        name="PV String 1 Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_pv1,
    ),
    GivEnergyInverterSensorDescription(
        key="p_pv2",
        name="PV String 2 Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_pv2,
    ),
    GivEnergyInverterSensorDescription(
        key="v_pv1",
        name="PV String 1 Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_pv1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="v_pv2",
        name="PV String 2 Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_pv2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="i_pv1",
        name="PV String 1 Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.i_pv1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="i_pv2",
        name="PV String 2 Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.i_pv2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="e_pv_day",
        name="PV Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv_day(),
    ),
    GivEnergyInverterSensorDescription(
        key="e_pv1_day",
        name="PV String 1 Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv1_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_pv2_day",
        name="PV String 2 Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv2_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_pv_total",
        name="PV Energy Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv_total,
    ),
    # --- Battery ---
    GivEnergyInverterSensorDescription(
        key="battery_soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.battery_soc,
    ),
    GivEnergyInverterSensorDescription(
        key="p_battery",
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_battery,
    ),
    GivEnergyInverterSensorDescription(
        key="v_battery",
        name="Battery Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_battery,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="i_battery",
        name="Battery Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.i_battery,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="t_battery",
        name="Battery Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.t_battery,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_charge_day",
        name="Battery Charge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_charge_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_discharge_day",
        name="Battery Discharge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_discharge_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_throughput",
        name="Battery Throughput Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_throughput,
    ),
    # --- Grid ---
    GivEnergyInverterSensorDescription(
        key="p_grid_out",
        name="Grid Export Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_grid_out,
    ),
    GivEnergyInverterSensorDescription(
        key="e_grid_out_day",
        name="Grid Export Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_grid_out_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_grid_in_day",
        name="Grid Import Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_grid_in_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_grid_out_total",
        name="Grid Export Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_grid_out_total,
    ),
    GivEnergyInverterSensorDescription(
        key="e_grid_in_total",
        name="Grid Import Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_grid_in_total,
    ),
    GivEnergyInverterSensorDescription(
        key="v_ac1",
        name="AC Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_ac1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="f_ac1",
        name="AC Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.f_ac1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Load / Consumption ---
    GivEnergyInverterSensorDescription(
        key="p_load_demand",
        name="Load Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_load_demand,
    ),
    GivEnergyInverterSensorDescription(
        key="e_load_day",
        name="Load Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_load_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_inverter_out_day",
        name="Inverter Output Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_inverter_out_day,
    ),
    GivEnergyInverterSensorDescription(
        key="e_inverter_out_total",
        name="Inverter Output Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_inverter_out_total,
    ),
    # --- Temperatures ---
    GivEnergyInverterSensorDescription(
        key="t_inverter_heatsink",
        name="Inverter Heatsink Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.t_inverter_heatsink,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="t_charger",
        name="Charger Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.t_charger,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Diagnostic ---
    GivEnergyInverterSensorDescription(
        key="work_time_total",
        name="Work Time Total",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: round(inv.work_time_total / 3600, 1),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

BATTERY_SENSORS: tuple[GivEnergyBatterySensorDescription, ...] = (
    GivEnergyBatterySensorDescription(
        key="soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.soc,
    ),
    GivEnergyBatterySensorDescription(
        key="v_out",
        name="Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.v_out,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        key="t_max",
        name="Temperature Max",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.t_max,
    ),
    GivEnergyBatterySensorDescription(
        key="t_min",
        name="Temperature Min",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.t_min,
    ),
    GivEnergyBatterySensorDescription(
        key="cap_remaining",
        name="Remaining Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.cap_remaining,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        key="cap_design",
        name="Design Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.cap_design,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        key="num_cycles",
        name="Charge Cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda bat: bat.num_cycles,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


COORDINATOR_SENSORS: tuple[GivEnergyCoordinatorSensorDescription, ...] = (
    GivEnergyCoordinatorSensorDescription(
        key="last_successful_refresh",
        name="Last Successful Refresh",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coord: coord.last_successful_refresh,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="consecutive_failures",
        name="Consecutive Refresh Failures",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coord: coord.consecutive_failures,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="total_failures",
        name="Total Refresh Failures",
        # Monotonically increasing within a coordinator instance; HA's recorder
        # treats the reset-to-zero on HA restart as a counter cycle, so the
        # long-term statistics still show correct cumulative deltas over time.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: coord.total_failures,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        GivEnergyInverterSensor(coordinator, description) for description in INVERTER_SENSORS
    ]

    for battery_index, battery in enumerate(coordinator.data.batteries):
        entities.extend(
            GivEnergyBatterySensor(coordinator, description, battery_index)
            for description in BATTERY_SENSORS
        )

    entities.extend(
        GivEnergyCoordinatorSensor(coordinator, description) for description in COORDINATOR_SENSORS
    )

    async_add_entities(entities)


class GivEnergyInverterSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergyInverterSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyInverterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy Inverter {serial}",
            manufacturer="GivEnergy",
            model=coordinator.data.inverter.model.name,
            sw_version=coordinator.data.inverter.firmware_version,
            serial_number=serial,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data.inverter)


class GivEnergyBatterySensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergyBatterySensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyBatterySensorDescription,
        battery_index: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._battery_index = battery_index
        battery = coordinator.data.batteries[battery_index]
        serial = battery.serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy Battery {serial}",
            manufacturer="GivEnergy",
            sw_version=str(battery.bms_firmware_version),
            serial_number=serial,
            via_device=(DOMAIN, coordinator.data.inverter_serial_number),
        )

    @property
    def native_value(self) -> Any:
        batteries = self.coordinator.data.batteries
        if self._battery_index >= len(batteries):
            return None
        return self.entity_description.value_fn(batteries[self._battery_index])


class GivEnergyCoordinatorSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    """Diagnostic sensor that reflects coordinator-level state (not plant data).

    Remains available even when the coordinator's last update failed, so
    the failure count and last-success timestamp are visible during outages.
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyCoordinatorSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyCoordinatorSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)
