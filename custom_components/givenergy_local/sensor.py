from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from givenergy_modbus.model.aio_battery import AioBatteryModule
from givenergy_modbus.model.battery import Battery, BatteryMaintenance
from givenergy_modbus.model.inverter import (
    BatteryCalibrationStage,
    BatteryType,
    MeterType,
    Model,
    Status,
    UsbDevice,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel, ir_register_age

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GivEnergyInverterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[InverterModel], Any] = field(default=lambda _: None)
    # If True, the entity is not created when value_fn returns None at first refresh.
    skip_if_none: bool = False
    # If True, the entity is not created on three-phase inverters, where the
    # underlying field is single-phase-only (e.g. a p_pv1+p_pv2 sum) and so would
    # be meaningless or surface as a permanently-unavailable orphan entity.
    single_phase_only: bool = False
    # If True, the entity's native_value is clamped to never decrease within a
    # session. Use for computed TOTAL_INCREASING sensors whose source value can
    # transiently dip due to multi-register polling skew.
    monotonic: bool = False
    # Model field the value_fn actually reads, when it differs from `key` (the
    # key is pinned by unique_id stability). Without this, renamed direct-register
    # sensors resolve no IR source and silently miss the stale-bank availability
    # check (#152, flagged on the #158 review). Leave None for true computed /
    # derived fields, which are deliberately untracked.
    source_field: str | None = None


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
class GivEnergyAioModuleSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AioBatteryModule], Any] = field(default=lambda _: None)


def _module_attr(name: str) -> Callable[[AioBatteryModule], Any]:
    """Return a value_fn that reads `name` off an AIO battery module.

    Per-module counterpart of `_battery_attr`; each closure captures its own
    attribute name so the bulk-defined per-cell entities don't share one.
    """
    return lambda module: getattr(module, name)


def _battery_hex(name: str, width: int) -> Callable[[Battery], Any]:
    """Return a value_fn that renders the named attr as a fixed-width hex string.

    The BMS status and warning registers carry bit-packed flags whose
    individual bit meanings aren't documented upstream yet. Rendering as
    `0xNN`/`0xNNNN` makes bit transitions visible at a glance in history
    rather than forcing the user to mentally decode decimal integers.

    Returns None for missing attributes so the sensor surfaces as
    `unknown` instead of raising during model construction or formatting.
    """

    def fn(bat: Battery) -> Any:
        val = getattr(bat, name, None)
        if val is None:
            return None
        # If upstream upgrades any of these fields to an Enum (the inverter's
        # usb_device_inserted has already gone that way), pull the underlying
        # numeric value before formatting.
        if hasattr(val, "value"):
            val = val.value
        try:
            return f"0x{int(val):0{width}X}"
        except TypeError, ValueError:
            return None

    return fn


@dataclass(frozen=True, kw_only=True)
class GivEnergyCoordinatorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GivEnergyUpdateCoordinator], Any] = field(default=lambda _: None)
    # None (not a dummy lambda) so sensors without attributes skip the call entirely.
    attributes_fn: Callable[[GivEnergyUpdateCoordinator], dict[str, Any] | None] | None = None


# `grid_power` (p_grid_out) is a single signed value, positive = export. HA's
# Energy Dashboard wants two always-positive power sensors (its "Two sensors"
# grid option), so split the direction out — mirroring how grid energy is
# already exposed as separate import/export totals. None passes through so the
# sensors read `unknown` rather than 0 before the first poll.
def _grid_export_power(inv: InverterModel) -> float | None:
    p = inv.p_grid_out
    return max(p, 0) if p is not None else None


def _grid_import_power(inv: InverterModel) -> float | None:
    p = inv.p_grid_out
    return max(-p, 0) if p is not None else None


INVERTER_SENSORS: tuple[GivEnergyInverterSensorDescription, ...] = (
    # --- Status ---
    GivEnergyInverterSensorDescription(
        key="status",
        name="Status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in Status],
        translation_key="inverter_status",
        # inv.status can be None while the library serves an empty model during
        # partial / pre-first-poll windows (givenergy-modbus's .inverter accessor
        # returns an empty model rather than raising); guard like the other enums.
        value_fn=lambda inv: inv.status.name.lower() if inv.status is not None else None,
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
    GivEnergyInverterSensorDescription(
        key="battery_calibration_stage",
        name="Battery Calibration Stage",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in BatteryCalibrationStage],
        translation_key="battery_calibration_stage",
        value_fn=lambda inv: (
            inv.battery_calibration_stage.name.lower()
            if inv.battery_calibration_stage is not None
            else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="inverter_fault_messages",
        name="Fault Messages",
        value_fn=lambda inv: (
            ", ".join(inv.inverter_fault_messages) if inv.inverter_fault_messages else None
        ),
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
        key="battery_maintenance_mode",
        name="Battery Maintenance Mode",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in BatteryMaintenance],
        translation_key="battery_maintenance_mode",
        # Only present on three-phase inverters (HR 1124); None on single-phase.
        skip_if_none=True,
        value_fn=lambda inv: (
            m.name.lower()
            if (m := getattr(inv, "battery_maintenance_mode", None)) is not None
            else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="usb_device_inserted",
        name="USB Device",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in UsbDevice],
        translation_key="usb_device_inserted",
        value_fn=lambda inv: (
            inv.usb_device_inserted.name.lower() if inv.usb_device_inserted is not None else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Solar / PV ---
    GivEnergyInverterSensorDescription(
        key="p_pv",
        name="PV Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        # Computed (p_pv1 + p_pv2) so not register-backed; watts -> 0 decimals.
        suggested_display_precision=0,
        value_fn=lambda inv: inv.p_pv(),
        single_phase_only=True,
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
        # Computed (e_pv1_day + e_pv2_day, each deci-scaled kWh) -> 1 decimal.
        suggested_display_precision=1,
        value_fn=lambda inv: inv.e_pv_day(),
        single_phase_only=True,
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
        # key kept (unique_id suffix); the library field was renamed to
        # e_battery_charge_today, routed per-model, in givenergy-modbus #76.
        key="e_battery_charge_day",
        name="Battery Charge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_charge_today,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_discharge_day",
        name="Battery Discharge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_discharge_today,
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
        key="grid_power",
        source_field="p_grid_out",
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.p_grid_out,
        # Signed, positive = export — the right shape for the bundled flow card
        # (which keys off it), but the opposite of HA's Energy-Dashboard sign and
        # awkward to read standalone. Hidden by default in favour of the split
        # import/export power sensors below; still recorded, so the flow card and
        # any existing user references keep working.
        entity_registry_visible_default=False,
    ),
    # Split, always-positive direction sensors for HA's Energy Dashboard "Two
    # sensors" grid-power option — no inversion helper (which would start its
    # long-term statistics from scratch). Named "Grid Power Import/Export" rather
    # than "Grid Import/Export Power" deliberately: the latter's `grid_export_power`
    # slug is the legacy entity_id of today's `grid_power` and is actively reclaimed
    # by the unique_id migration (see _RENAMED_UNIQUE_ID_SUFFIXES), so reusing it
    # would collide.
    GivEnergyInverterSensorDescription(
        key="grid_power_import",
        source_field="p_grid_out",
        name="Grid Power Import",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grid_import_power,
    ),
    GivEnergyInverterSensorDescription(
        key="grid_power_export",
        source_field="p_grid_out",
        name="Grid Power Export",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grid_export_power,
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
        skip_if_none=True,
        value_fn=lambda inv: inv.p_grid_out_ph1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="i_grid_port",
        name="Grid Port Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.i_grid_port,
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
        # House consumption, sourced per model (#154). Single-phase inverters
        # expose no consumption register; givenergy-modbus derives it (PV gen +
        # grid-in - grid-out - AC-charge) to match the GE app's "Consumption
        # today". Three-phase units meter it natively instead (e_load_today,
        # IR 1396-1397 — modelled in givenergy-modbus 2.2 but not yet validated
        # on real 3-phase hardware), so the value_fn falls back to that there.
        # Same key on both topologies keeps the dashboard strategy and
        # recommended-entity list working unchanged.
        # monotonic=True because the derived value is computed from several
        # registers polled at slightly different times; a reading can
        # transiently dip by a few Wh when one component updates before the
        # others, tripping TOTAL_INCREASING's strictly-increasing guard (#142).
        key="e_consumption_today",
        name="House Consumption Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        monotonic=True,
        value_fn=lambda inv: getattr(
            inv, "e_consumption_today", getattr(inv, "e_load_today", None)
        ),
        skip_if_none=True,
        # Resolves per-model: e_load_today isn't in the single-phase LUT (the
        # derived value stays untracked for staleness), but on three-phase it
        # names the native register the value_fn falls back to (#152/#158).
        source_field="e_load_today",
    ),
    GivEnergyInverterSensorDescription(
        # Native lifetime consumption counter (IR 1398-1399) — three-phase
        # only; single-phase models lack the field, so skip_if_none drops it
        # there. No monotonic clamp: this is a single metered register, not a
        # multi-register derivation.
        key="e_load_total",
        name="House Consumption Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: getattr(inv, "e_load_total", None),
        skip_if_none=True,
    ),
    GivEnergyInverterSensorDescription(
        # Renamed from "Load Energy Today" / e_load_day (givenergy-modbus #174):
        # IR35 was a GivTCP-era mislabel — it has always been AC charge, not house
        # load. A unique_id migration in __init__.py carries the existing history
        # across so it lands under the correct name.
        key="e_ac_charge_today",
        name="AC Charge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_ac_charge_today,
    ),
    GivEnergyInverterSensorDescription(
        # IR44 is PV generation, not inverter AC output. givenergy-modbus #174
        # renamed e_inverter_out_day -> e_pv_generation_today (single-phase). The
        # total (IR45/46) was confirmed as PV-generation-total in #176 and renamed
        # e_inverter_out_total -> e_pv_generation_total. Both entity keys and names
        # move together; unique_id migrations in __init__.py carry existing history.
        key="e_pv_generation_today",
        name="PV Generation Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv_generation_today,
    ),
    GivEnergyInverterSensorDescription(
        key="e_pv_generation_total",
        name="PV Generation Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_pv_generation_total,
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
    # --- Lifetime battery energy totals (routed per-model in givenergy-modbus
    # #76; return None on models with no known total register — e.g. AC-coupled
    # — so they're skipped there rather than shown blank). These replace the
    # provisional `e_battery_*_alt` sensors, which were an anomaly: their keys
    # change, so any pre-existing "Battery Alt …" entities orphan and the user
    # removes them. Acceptable churn at this pre-release stage. ---
    GivEnergyInverterSensorDescription(
        key="e_battery_charge_total",
        name="Battery Charge Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_charge_total,
        skip_if_none=True,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_discharge_total",
        name="Battery Discharge Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_discharge_total,
        skip_if_none=True,
    ),
    # --- Solar diverter ---
    GivEnergyInverterSensorDescription(
        key="e_solar_diverter",
        name="Solar Diverter Energy Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        skip_if_none=True,
        value_fn=lambda inv: inv.e_solar_diverter,
    ),
    # --- DC bus voltages ---
    GivEnergyInverterSensorDescription(
        key="v_p_bus",
        name="Positive DC Bus Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_p_bus,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="v_n_bus",
        name="Negative DC Bus Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.v_n_bus,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- EPS / Generation ---
    GivEnergyInverterSensorDescription(
        key="p_backup",
        name="Backup Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        skip_if_none=True,
        value_fn=lambda inv: inv.p_backup,
    ),
    GivEnergyInverterSensorDescription(
        key="p_combined_generation",
        name="Combined Generation Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        skip_if_none=True,
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
        source_field="work_time_total_hours",
        name="Work Time Total",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        # Library field is work_time_total_hours (uint32, whole hours); the bare
        # work_time_total alias is deprecated (#84) and slated for removal in 3.0.
        suggested_display_precision=0,
        value_fn=lambda inv: inv.work_time_total_hours,
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
        single_phase_only=True,
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
        key="cap_calibrated",
        name="Calibrated Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: bat.cap_calibrated,
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
    # BMS status and warning flag bytes — no enum mapping exists upstream yet,
    # but rendering them as hex lets users spot bit transitions in history
    # without having to mentally decode the decimal forms. Bitmap state
    # values aren't valid `MEASUREMENT` data, so we deliberately omit the
    # state_class to keep them out of long-term statistics.
    *(
        GivEnergyBatterySensorDescription(
            key=f"status_{i}",
            name=f"BMS Status {i}",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_battery_hex(f"status_{i}", width=2),
        )
        for i in range(1, 8)
    ),
    GivEnergyBatterySensorDescription(
        key="warning_1",
        name="BMS Warning 1",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_battery_hex("warning_1", width=2),
    ),
    GivEnergyBatterySensorDescription(
        key="warning_2",
        name="BMS Warning 2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_battery_hex("warning_2", width=2),
    ),
    GivEnergyBatterySensorDescription(
        key="bms_firmware_version",
        name="BMS Firmware Version",
        value_fn=lambda bat: bat.bms_firmware_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        # Alternate copy of the design capacity, populated on some firmware
        # variants. Exposed alongside `cap_design` so users can spot when
        # the two diverge — that's the same alt-source phenomenon we saw
        # on the inverter side (the battery_2_* fields). See modbus#76.
        key="cap_design2",
        # No parens in the display name — keeps the auto-generated entity_id
        # predictable as `design_capacity_alt` (slugify drops parentheses but
        # leaves stray underscores around them, which the dashboard template
        # would then have to special-case).
        name="Design Capacity Alt",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bat: getattr(bat, "cap_design2", None),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyBatterySensorDescription(
        # Per-battery USB-device register. Semantics are partially unverified
        # upstream — manufacturer docs only define 0 and 8 but values like 11
        # have been observed on D0.449-A0.449. Rendered as hex so unrecognised
        # values are still readable as bit patterns rather than mystery
        # decimals.
        key="usb_device_inserted",
        name="USB Device",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_battery_hex("usb_device_inserted", width=4),
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


# All-in-One per-module battery sensors (#192). Each removable module reports
# its own 24 cell voltages and per-cell temperatures. Mirrors the LV per-cell
# entities above: DIAGNOSTIC, enabled by default. Cell temps 13-24 read zero on
# known AIO hardware, so only 01-12 are exposed (matching what the module BMS
# actually populates); voltages cover all 24 cells.
AIO_MODULE_SENSORS: tuple[GivEnergyAioModuleSensorDescription, ...] = (
    *(
        GivEnergyAioModuleSensorDescription(
            key=f"v_cell_{i:02d}",
            name=f"Cell {i} Voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_module_attr(f"v_cell_{i:02d}"),
        )
        for i in range(1, 25)
    ),
    *(
        GivEnergyAioModuleSensorDescription(
            key=f"t_cell_{i:02d}",
            name=f"Cell {i} Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_module_attr(f"t_cell_{i:02d}"),
        )
        for i in range(1, 13)
    ),
)


def _partial_failure_attributes(
    coordinator: GivEnergyUpdateCoordinator,
) -> dict[str, Any] | None:
    """Summarise the most recent partial poll's failed reads for the UI.

    Names the device(s) that dropped (e.g. "0x34" for a battery) plus the
    per-bank detail, so a flaky device can be identified even though its
    entities stay available with stale data.
    """
    failures = coordinator.last_partial_failures
    if not failures:
        return None
    return {
        "last_failed_devices": sorted({f"0x{f.device_address:02x}" for f in failures}),
        "last_failure_count": len(failures),
        "last_failures": [
            f"0x{f.device_address:02x} "
            f"{getattr(f.request_type, 'value', f.request_type)} "
            f"@ {f.base_register}+{f.register_count}"
            for f in failures
        ],
    }


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
    GivEnergyCoordinatorSensorDescription(
        key="partial_failures",
        name="Partial Refresh Failures",
        # Polls that returned data but had some register reads fail. The
        # attributes name the device(s) that dropped — the only UI signal of a
        # flaky device, since its entities stay available with frozen values.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: coord.partial_failures,
        attributes_fn=_partial_failure_attributes,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def _include_inverter_sensor(
    description: GivEnergyInverterSensorDescription,
    inverter: InverterModel,
    is_three_phase: bool,
) -> bool:
    """Whether to create an inverter sensor at setup.

    `single_phase_only` descriptions are dropped on three-phase inverters, where
    the underlying field is single-phase-only — gating on plant topology rather
    than a runtime None avoids suppressing a genuine single-phase sensor during a
    transient partial first read.

    `skip_if_none` descriptions have their `value_fn` evaluated eagerly here, so a
    single bad descriptor — e.g. a library field renamed out from under us — must
    not be allowed to raise and abort the *entire* sensor platform (which is how a
    field rename in givenergy-modbus once dropped every sensor). Guard the call:
    skip the offending sensor with a warning, but keep the rest.
    """
    if description.single_phase_only and is_three_phase:
        return False
    if not description.skip_if_none:
        return True
    try:
        return description.value_fn(inverter) is not None
    except Exception:  # noqa: BLE001 - one bad descriptor must not sink the platform
        _LOGGER.warning(
            "Skipping sensor %s: its value_fn raised at setup (library field drift?)",
            description.key,
            exc_info=True,
        )
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    inverter = coordinator.data.inverter
    capabilities = coordinator.data.capabilities
    is_three_phase = bool(capabilities and capabilities.is_three_phase)

    entities: list[SensorEntity] = [
        GivEnergyInverterSensor(coordinator, description)
        for description in INVERTER_SENSORS
        if _include_inverter_sensor(description, inverter, is_three_phase)
    ]

    for battery_index, battery in enumerate(coordinator.data.batteries):
        entities.extend(
            GivEnergyBatterySensor(coordinator, description, battery_index)
            for description in BATTERY_SENSORS
        )

    # AIO per-module battery devices (#192) — empty on non-AIO plants. A module
    # with a blank/invalid serial can't anchor a device, so skip it; the index
    # still aligns with `aio_battery_modules` for the entities we do create.
    # Like the battery entities, this set is fixed at setup: a module absent
    # during the initial probe gets no entities until a reload (tracked in #148,
    # to be fixed uniformly for all device types).
    for module_index, module in enumerate(coordinator.data.aio_battery_modules):
        if not module.is_valid():
            continue
        entities.extend(
            GivEnergyAioModuleSensor(coordinator, description, module_index)
            for description in AIO_MODULE_SENSORS
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


def _device_kind(model: Model) -> str:
    """The device-name noun, which also drives the entity_id prefix.

    Buckets to one of Inverter / EMS / Gateway — NOT the fine-grained model name —
    so every actual inverter stays "GivEnergy Inverter {serial}" (unchanged), while
    an EMS controller / gateway gets its own identity ("GivEnergy EMS …" →
    `givenergy_ems_…` entity ids). This is the single place that decides a device's
    kind; when the typed-plant model lands (modbus#106) it swaps its source here
    (model → plant device type) without touching anything downstream.
    """
    if model is Model.EMS:
        return "EMS"
    if model is Model.GATEWAY:
        return "Gateway"
    return "Inverter"


def _derive_display_precision(description: SensorEntityDescription, model: Any) -> int | None:
    """Native display precision for a sensor, from the library's register scaling.

    Returns None (leave HA's default) when:
    - the description already pins a precision — an explicit value always wins;
    - the sensor has no ``state_class`` — display precision only applies to
      numeric measurement/total sensors, never to the enum / hex-string /
      version diagnostics, several of which are register-backed integers that
      hass deliberately renders as non-numeric strings;
    - the library has no precision for the attribute (non-numeric register, or a
      computed value not backed by a single register).
    """
    if description.suggested_display_precision is not None:
        return None
    if description.state_class is None:
        return None
    return model.precision_of(description.key)


# A drop larger than this (in the sensor's native unit) is treated as a
# genuine source reset rather than a transient polling artefact. Covers the
# case where the inverter clock lags HA's local midnight by one scan interval,
# so the actual counter reset arrives on a subsequent read after the day
# boundary has already been committed. The value is intentionally conservative:
# transient multi-register skew is a few Wh; a 500 Wh floor is far above any
# realistic polling noise while still detecting any meaningful midnight reset.
_MONOTONIC_RESET_THRESHOLD = 0.5  # kWh (matches e_consumption_today's native unit)

# A backing input-register bank that has stopped committing past this ceiling
# reads as a fault, not polling jitter (#152): stale-but-plausible values
# silently masking a dead bank are worse than going unavailable. Expressed as
# max(floor, scans × scan interval) so longer-interval installs scale the
# ceiling rather than false-flagging on cadence — at the 30 s default that's
# ten missed commits.
_STALE_IR_CEILING_FLOOR = 300.0  # seconds
_STALE_IR_CEILING_SCANS = 10


def _source_ir_registers(model_cls: type, key: str) -> tuple[int, ...]:
    """The input-register indexes backing ``key`` on ``model_cls``, () if none.

    Resolved through the model's register LUT, so it tracks whatever layout the
    concrete model (single/three-phase, …) actually has — never a hardcoded
    bank list. Computed fields aren't in the LUT and HR-backed fields are
    filtered out (HR banks are only re-read every few ticks by design, so
    their age is not a staleness signal); both resolve to () and keep the
    default coordinator availability. Mock model classes in tests also land
    here, via the getattr fallbacks.
    """
    getter = getattr(model_cls, "REGISTER_GETTER", None)
    lut = getattr(getter, "REGISTER_LUT", None)
    defn = lut.get(key) if lut is not None else None
    if defn is None:
        return ()
    # Register._idx is the library's storage for the numeric index; there's no
    # public accessor yet (givenergy-modbus is a sister project, tracked there).
    return tuple(r._idx for r in defn.registers if type(r).__name__ == "IR")


class GivEnergyInverterSensor(
    CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity, RestoreEntity
):
    _attr_has_entity_name = True
    entity_description: GivEnergyInverterSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyInverterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._monotonic_max: float | None = None
        self._monotonic_date: date | None = None
        self._source_ir_registers = _source_ir_registers(
            type(coordinator.data.inverter), description.source_field or description.key
        )
        interval = coordinator.update_interval
        self._stale_ir_ceiling = max(
            _STALE_IR_CEILING_FLOOR,
            _STALE_IR_CEILING_SCANS * (interval.total_seconds() if interval else 0.0),
        )
        precision = _derive_display_precision(description, coordinator.data.inverter)
        if precision is not None:
            self._attr_suggested_display_precision = precision
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
            manufacturer="GivEnergy",
            model=_MODEL_NAMES.get(model, model.name),
            sw_version=coordinator.data.inverter.firmware_version,
            serial_number=serial,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self.entity_description.monotonic:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        try:
            restored = float(last_state.state)
        except ValueError, TypeError:
            return
        last_date = dt_util.as_local(last_state.last_updated).date()
        if last_date == dt_util.now().date():
            # Seed the intra-day max from the last persisted value so a
            # transient dip on the first post-restart reading doesn't become
            # the new baseline while the recorder still holds the higher value.
            self._monotonic_max = restored
            self._monotonic_date = last_date

    @property
    def available(self) -> bool:
        """Drop to unavailable when a backing IR bank has stopped committing (#152).

        Keyed off the plant's stamped block windows generically — which banks a
        device serves varies by model and address. Sensors with no resolvable
        IR source (computed fields, HR-backed config) keep the default
        coordinator availability, as does a never-committed bank.
        """
        if not super().available:
            return False
        if not self._source_ir_registers:
            return True
        capabilities = self.coordinator.data.capabilities
        if capabilities is None:
            return True
        for register in self._source_ir_registers:
            age = ir_register_age(self.coordinator.data, capabilities.inverter_address, register)
            if age is not None and age > self._stale_ir_ceiling:
                return False
        return True

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value_fn(self.coordinator.data.inverter)
        if self.entity_description.monotonic and isinstance(value, (int, float)):
            today = dt_util.now().date()
            if self._monotonic_date != today:
                # New calendar day in HA's timezone: start fresh so the
                # midnight reset passes through as a real decrease.
                self._monotonic_max = value
                self._monotonic_date = today
            elif (
                self._monotonic_max is not None
                and value < self._monotonic_max - _MONOTONIC_RESET_THRESHOLD
            ):
                # Large same-day drop: treat as a genuine source reset.
                # This handles an inverter clock that lags HA's midnight by
                # one poll cycle — the actual counter reset arrives on a
                # subsequent read after date tracking has already committed
                # the new day, so we must detect it by magnitude rather than
                # date alone.
                self._monotonic_max = value
            else:
                self._monotonic_max = max(value, self._monotonic_max or value)
            return self._monotonic_max
        return value


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
        precision = _derive_display_precision(description, battery)
        if precision is not None:
            self._attr_suggested_display_precision = precision
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


class GivEnergyAioModuleSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    """Per-cell sensor for one All-in-One removable battery module (#192).

    Each module is its own HA device, linked to the AIO inverter as parent via
    `via_device`, identified by the module's `HX…` serial.
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyAioModuleSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyAioModuleSensorDescription,
        module_index: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Bind to the module's serial, not its list position. aio_battery_modules
        # is rebuilt every refresh from whichever module caches decoded, so indices
        # shift when a module drops out — resolving by serial keeps each entity tied
        # to its own module instead of cross-wiring to a neighbour's cell data.
        serial = coordinator.data.aio_battery_modules[module_index].serial_number
        self._module_serial = serial
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy Battery Module {serial}",
            manufacturer="GivEnergy",
            model="AIO Battery Module",
            serial_number=serial,
            via_device=(DOMAIN, coordinator.data.inverter_serial_number),
        )

    def _module(self) -> AioBatteryModule | None:
        """Resolve this entity's module by serial in the latest coordinator data."""
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (m for m in data.aio_battery_modules if m.serial_number == self._module_serial),
            None,
        )

    @property
    def available(self) -> bool:
        # Unavailable (not cross-wired) when this module is absent from the poll.
        return super().available and self._module() is not None

    @property
    def native_value(self) -> Any:
        module = self._module()
        if module is None:
            return None
        return self.entity_description.value_fn(module)


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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator)
