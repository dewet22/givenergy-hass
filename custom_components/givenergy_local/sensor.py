from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from givenergy_modbus.model.battery import Battery
from givenergy_modbus.model.inverter import BatteryType, MeterType, Model, SinglePhaseInverter, Status
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
    value_fn: Callable[[SinglePhaseInverter], Any] = field(default=lambda _: None)


@dataclass(frozen=True, kw_only=True)
class GivEnergyBatterySensorDescription(SensorEntityDescription):
    value_fn: Callable[[Battery], Any] = field(default=lambda _: None)


def _battery_attr(name: str) -> Callable[[Battery], Any]:
    """Return a value_fn that reads `name` off the battery.

    Used by the bulk-defined per-cell entities so each closure captures
    its own attribute name explicitly (and so mypy can infer the type
    of the resulting Callable).
    """
    return lambda bat: getattr(bat, name)


@dataclass(frozen=True, kw_only=True)
class GivEnergyCoordinatorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GivEnergyUpdateCoordinator], Any] = field(default=lambda _: None)


INVERTER_SENSORS: tuple[GivEnergyInverterSensorDescription, ...] = (
    # --- Status ---
    GivEnergyInverterSensorDescription(
        key="status",
        name="Status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in Status],
        translation_key="inverter_status",
        value_fn=lambda inv: inv.status.name.lower(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="fault_code",
        name="Fault Code",
        value_fn=lambda inv: inv.fault_code,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="inverter_errors",
        name="Inverter Errors",
        value_fn=lambda inv: inv.inverter_errors,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="charger_warning_code",
        name="Charger Warning Code",
        value_fn=lambda inv: inv.charger_warning_code,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Raw integers — the upstream library doesn't ship enum mappings for
    # these yet, but exposing the values lets users build templates or
    # see them change in history while the mappings get figured out.
    GivEnergyInverterSensorDescription(
        key="charge_status",
        name="Charge Status",
        value_fn=lambda inv: inv.charge_status,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="system_mode",
        name="System Mode",
        value_fn=lambda inv: inv.system_mode,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_pause_mode",
        name="Battery Pause Mode",
        value_fn=lambda inv: inv.battery_pause_mode,
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
    GivEnergyInverterSensorDescription(
        key="v_ac1_output",
        name="AC Output Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_ac1_output,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="f_ac1_output",
        name="AC Output Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.f_ac1_output,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="i_ac1",
        name="AC Output Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.i_ac1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="p_grid_apparent",
        name="Grid Apparent Power",
        native_unit_of_measurement="VA",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_grid_apparent,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="pf_inverter_output_now",
        name="Inverter Power Factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.pf_inverter_output_now,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="p_grid_out_ph1",
        name="Grid Power Phase 1",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_grid_out_ph1,
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
    GivEnergyInverterSensorDescription(
        key="e_inverter_export_total",
        name="Inverter Export Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_inverter_export_total,
    ),
    GivEnergyInverterSensorDescription(
        key="e_inverter_in_total",
        name="Charge from Grid Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_inverter_in_total,
    ),
    GivEnergyInverterSensorDescription(
        key="e_discharge_year",
        name="Battery Discharge This Year",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_discharge_year,
    ),
    # --- EPS / Generation ---
    GivEnergyInverterSensorDescription(
        key="p_backup",
        name="Backup Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_backup,
    ),
    GivEnergyInverterSensorDescription(
        key="p_combined_generation",
        name="Combined Generation Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_combined_generation,
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
    GivEnergyInverterSensorDescription(
        key="device_type_code",
        name="Device Type Code",
        value_fn=lambda inv: inv.device_type_code,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="num_mppt",
        name="MPPT Count",
        value_fn=lambda inv: inv.num_mppt,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="num_phases",
        name="Phase Count",
        value_fn=lambda inv: inv.num_phases,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="arm_firmware_version",
        name="ARM Firmware Version",
        value_fn=lambda inv: inv.arm_firmware_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="dsp_firmware_version",
        name="DSP Firmware Version",
        value_fn=lambda inv: inv.dsp_firmware_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="modbus_version",
        name="Modbus Version",
        value_fn=lambda inv: inv.modbus_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="meter_type",
        name="Meter Type",
        device_class=SensorDeviceClass.ENUM,
        options=[m.name.lower() for m in MeterType],
        translation_key="meter_type",
        value_fn=lambda inv: inv.meter_type.name.lower() if inv.meter_type is not None else None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_type",
        name="Battery Type",
        device_class=SensorDeviceClass.ENUM,
        options=[m.name.lower() for m in BatteryType],
        translation_key="battery_type",
        value_fn=lambda inv: (
            inv.battery_type.name.lower() if inv.battery_type is not None else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_capacity_ah",
        name="Battery Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.battery_capacity_ah,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_capacity_kwh",
        name="Battery Nominal Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda inv: inv.battery_capacity_kwh,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

BATTERY_SENSORS: tuple[GivEnergyBatterySensorDescription, ...] = (
    GivEnergyBatterySensorDescription(
        # Entity name is just "SOC" because the device is already named
        # "GivEnergy Battery <serial>"; "Battery SOC" would be redundant.
        key="soc",
        name="SOC",
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
    # --- BMS internals (pack health monitoring) ---
    GivEnergyBatterySensorDescription(
        key="num_cells",
        name="Cell Count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.num_cells,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        key="v_cells_sum",
        name="Cell Voltages Sum",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda bat: bat.v_cells_sum,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        key="t_bms_mosfet",
        name="BMS MOSFET Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.t_bms_mosfet,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Per-cell voltages — 16 entities; unused cells in smaller packs read ~0.
    # `attr` default-arg captures the loop variable to avoid the closure trap.
    *(
        GivEnergyBatterySensorDescription(
            key=f"v_cell_{i:02d}",
            name=f"Cell {i} Voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_battery_attr(f"v_cell_{i:02d}"),
        )
        for i in range(1, 17)
    ),
    # Cell temperatures are reported in 4-cell groups (the BMS only samples one
    # thermistor per group, not per cell).
    *(
        GivEnergyBatterySensorDescription(
            key=f"t_cells_{a:02d}_{b:02d}",
            name=f"Cells {a}-{b} Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_battery_attr(f"t_cells_{a:02d}_{b:02d}"),
        )
        for a, b in [(1, 4), (5, 8), (9, 12), (13, 16)]
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


_MODEL_NAMES: dict[Model, str] = {
    Model.HYBRID: "Hybrid",
    Model.AC: "AC",
    Model.HYBRID_3PH: "Hybrid (3-phase)",
    Model.EMS: "EMS",
    Model.AC_3PH: "AC (3-phase)",
    Model.GATEWAY: "Gateway",
    Model.ALL_IN_ONE: "All In One",
}


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
            model=_MODEL_NAMES.get(
                coordinator.data.inverter.model, coordinator.data.inverter.model.name
            ),
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
