from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from givenergy_modbus.model.aio_battery import AioBatteryModule
from givenergy_modbus.model.battery import Battery, BatteryMaintenance
from givenergy_modbus.model.devices import InverterSummary
from givenergy_modbus.model.ems import Ems
from givenergy_modbus.model.hv_bcu import Bcu, Bmu, HvStack
from givenergy_modbus.model.inverter import (
    BatteryCalibrationStage,
    BatteryType,
    ChargeStatus,
    MeterType,
    Model,
    Status,
    UsbDevice,
)
from givenergy_modbus.model.register import Register
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

from .const import (
    CONF_BATTERY_DATA_ONLY,
    CONF_EXPOSE_PER_CELL,
    DEFAULT_BATTERY_DATA_ONLY,
    DOMAIN,
)
from .coordinator import GivEnergyUpdateCoordinator, InverterModel

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GivEnergyInverterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[InverterModel], Any] = field(default=lambda _: None)
    # If True, the entity is not created when value_fn returns None at first refresh.
    skip_if_none: bool = False
    # If True, the entity is not created on an AIO (battery-only) plant, where the
    # field reads a meaningless value rather than None (MPPT count 0, a solar
    # diverter total, a static yearly discharge figure), so skip_if_none can't
    # catch it (#95).
    skip_if_aio: bool = False
    # If True, the entity is not created on an EMS plant. The 0x11 controller's
    # direct registers are mostly meaningful (PV/grid/battery/AC), but a couple of
    # load figures are controller-local — House Consumption's per-unit derivation
    # and the inverter busbar load (p_load_demand) — and are superseded by the EMS
    # calc/measured-load aggregates, so gate just those (#201).
    skip_if_ems: bool = False
    # If True, the entity is not created on three-phase inverters, where the
    # underlying field is single-phase-only (e.g. a p_pv1+p_pv2 sum) and so would
    # be meaningless or surface as a permanently-unavailable orphan entity.
    single_phase_only: bool = False
    # If True, the entity's native_value is clamped to never decrease within a
    # session. Use for computed TOTAL_INCREASING sensors whose source value can
    # transiently dip due to multi-register polling skew.
    monotonic: bool = False
    # Only meaningful with monotonic=True. True (default) runs the daily-counter
    # clamp, which admits the genuine midnight reset back to ~0. False runs a pure
    # high-water clamp for lifetime counters that never reset: it never re-baselines
    # at the day boundary, so a midnight dip can't surface as a fake meter reset in
    # HA statistics (#223).
    monotonic_resets_daily: bool = True
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


@dataclass(frozen=True, kw_only=True)
class GivEnergyHvStackSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Bcu], Any] = field(default=lambda _: None)


def _bcu_attr(name: str) -> Callable[[Bcu], Any]:
    """Return a value_fn that reads `name` off an HV battery stack's BCU.

    Per-stack counterpart of `_battery_attr`/`_module_attr`; like them it reads
    without a default, so a renamed/typo'd field (library drift) fails loudly in
    tests rather than silently surfacing as `unknown`. The BCU fields are
    guaranteed present by the pinned givenergy-modbus model.
    """
    return lambda bcu: getattr(bcu, name)


@dataclass(frozen=True, kw_only=True)
class GivEnergyHvModuleSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Bmu], Any] = field(default=lambda _: None)


def _hv_module_attr(name: str) -> Callable[[Bmu], Any]:
    """Return a value_fn that reads `name` off an HV battery module (BMU).

    Per-module counterpart of `_module_attr` for the bulk-defined per-cell
    entities so each closure captures its own attribute name.
    """
    return lambda bmu: getattr(bmu, name)


def _present_cells(obj: Any, prefix: str, count: int) -> list[Any]:
    """Non-zero, non-None cell readings `prefix_01`..`prefix_{count}` off `obj`.

    Unused cells in a partially-populated pack read ~0, so they're excluded from
    the roll-ups below rather than dragging the min to zero. Values come off the
    model via getattr, so they're typed Any like the other value_fns here.
    """
    return [
        v
        for i in range(1, count + 1)
        if (v := getattr(obj, f"{prefix}_{i:02d}", None)) not in (None, 0)
    ]


def _cell_rollup(prefix: str, count: int, fn: Callable[[list[Any]], Any]) -> Callable[[Any], Any]:
    """A value_fn reducing the present `prefix` cells via `fn` (min/max/spread).

    Returns None when no cell has a usable reading, so the entity reports
    `unknown` rather than a misleading 0.
    """

    def value(obj: Any) -> float | None:
        cells = _present_cells(obj, prefix, count)
        return fn(cells) if cells else None

    return value


@dataclass(frozen=True, kw_only=True)
class GivEnergyManagedInverterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[InverterSummary], Any] = field(default=lambda _: None)


@dataclass(frozen=True, kw_only=True)
class GivEnergyEmsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Ems], Any] = field(default=lambda _: None)


def _summary_attr(name: str) -> Callable[[InverterSummary], Any]:
    """Return a value_fn that reads `name` off an EMS managed-inverter summary.

    Counterpart of `_module_attr`/`_bcu_attr` for the blinded `InverterSummary`
    rollup an EMS controller exposes per managed inverter.
    """
    return lambda summary: getattr(summary, name)


def _managed_status(value: Any) -> str | None:
    """Render an EMS managed inverter's status defensively.

    Unlike a directly-reachable inverter's Status enum, the EMS rollup reports
    each managed inverter's status as a raw string, so coerce instead of
    assuming `.name` — calling `.name` on the string raised on every poll on
    real EMS hardware (#52).
    """
    if value is None:
        return None
    return value.name if hasattr(value, "name") else str(value)


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
        # None on AIO (HR223/224 unpopulated) — drop rather than show "Unknown" (#194).
        skip_if_none=True,
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
        # Deliberately NOT skip_if_none: value_fn returns None for the healthy
        # *empty fault list* too, not just the unsupported-on-AIO case. Skipping
        # would drop this on a healthy single-phase install at setup, leaving a
        # later fault nowhere to surface until a reload (Codex review, #194).
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ChargeStatus mapping shipped in modbus 2.3.1 (#222) — render the
    # friendly label; unknown codes read as unknown rather than a bare int.
    # system_mode is still a raw integer (stringified, so a future enum
    # rendering isn't an intrusive change) — no library mapping yet.
    GivEnergyInverterSensorDescription(
        key="charge_status",
        name="Charge Status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in ChargeStatus],
        translation_key="charge_status",
        value_fn=lambda inv: (
            s.name.lower() if (s := getattr(inv, "charge_status_label", None)) is not None else None
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="system_mode",
        name="System Mode",
        value_fn=lambda inv: inv.system_mode,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Battery topology nameplate (HR308-310, givenergy-modbus 2.6.0). Static ratings,
    # not live telemetry, so DIAGNOSTIC + no state_class. skip_if_none drops them where
    # the register doesn't decode (None in the modbus fixtures; populated on inverters
    # that poll the HR300 block). NB power/current scale is the library's most-likely
    # raw-uint16 reading, flagged "unconfirmed on live hardware" upstream — confirm
    # against real values before relying on the units. max_charge_pct is self-validating.
    GivEnergyInverterSensorDescription(
        key="battery_nominal_power",
        name="Battery Nominal Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        value_fn=lambda inv: getattr(inv, "battery_nominal_power", None),
        skip_if_none=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_nominal_current",
        name="Battery Nominal Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=lambda inv: getattr(inv, "battery_nominal_current", None),
        skip_if_none=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_max_charge_pct",
        name="Battery Max Charge Percentage",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda inv: getattr(inv, "battery_max_charge_pct", None),
        skip_if_none=True,
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
        # Three-phase units inherit this single-phase register address but their
        # firmware never populates it, so it reads frozen rather than unavailable
        # (#174). Real 3ph battery temperature comes from the HV cluster
        # (Bcu.cluster_cell_temperature) once HV-stack support lands (#179).
        single_phase_only=True,
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
        # The EMS controller (Model.EMS_COMMERCIAL) isn't in givenergy-modbus's
        # per-model battery-energy source map, so this computed field resolves to
        # None there and the sensor renders "unknown" — it's an inverter-level
        # register the 0x11 controller doesn't populate. Gate it off EMS (#221).
        skip_if_ems=True,
    ),
    GivEnergyInverterSensorDescription(
        key="e_battery_discharge_day",
        name="Battery Discharge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda inv: inv.e_battery_discharge_today,
        # Same as Battery Charge Today: None on the EMS controller, so gate it
        # off to avoid a permanently-"unknown" sensor on that device (#221).
        skip_if_ems=True,
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
        # Controller-local on an EMS (IR42 inverter busbar load): on the capture it
        # reads 48 W vs the EMS calc_load_power 489 W, so it misrepresents the plant
        # load. The EMS load aggregates are authoritative there (#201).
        skip_if_ems=True,
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
        # Derived (inverter-battery-grid) — wrong on an EMS controller, where it
        # computes a per-controller figure (e.g. -9.8) rather than whole-plant load;
        # the EMS load aggregates cover it there instead (#201).
        skip_if_ems=True,
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
        # Self-consumption — PV generation used on-site, derived in givenergy-modbus
        # 2.5.12 as max(0, pv_generation - grid_export) on SinglePhaseInverter only;
        # skip_if_none drops it on three-phase, which lacks the field (#223).
        # monotonic=True because it's a difference of two cumulative day counters
        # (PV today, export today) read with poll skew, and genuinely dips when the
        # battery exports to grid — either way a TOTAL_INCREASING daily counter must
        # be clamped against the transient/real decrease (same rationale as House
        # Consumption Today). The clamp holds the high-water mark, which slightly
        # overstates self-consumption across a battery-to-grid window; that is the
        # price of a monotonic Energy-dashboard total and is the library's documented
        # contract for this field.
        key="e_self_consumption_today",
        name="Self Consumption Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        monotonic=True,
        value_fn=lambda inv: getattr(inv, "e_self_consumption_today", None),
        skip_if_none=True,
        # Derived PV-minus-export figure; gate off EMS controllers where the
        # per-controller derivation isn't validated, mirroring House Consumption
        # Today (#201/#223). Revisit once confirmed on EMS hardware.
        skip_if_ems=True,
    ),
    GivEnergyInverterSensorDescription(
        # Lifetime self-consumption (max(0, pv_total - export_total), modbus 2.5.12).
        # Also a difference of cumulative counters that can transiently dip / decrease
        # on battery-to-grid export, so it carries a monotonic clamp too — but the
        # non-daily (pure high-water) variant: a lifetime counter never resets, so the
        # daily clamp's midnight re-baseline would expose an overnight dip as a fake
        # meter reset in HA statistics (#223).
        key="e_self_consumption_total",
        name="Self Consumption Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        monotonic=True,
        monotonic_resets_daily=False,
        value_fn=lambda inv: getattr(inv, "e_self_consumption_total", None),
        skip_if_none=True,
        skip_if_ems=True,
    ),
    GivEnergyInverterSensorDescription(
        # PV that reached load directly (bypassing battery and grid), modbus 2.5.13.
        # DC-coupled hybrids only — returns None on AC-coupled/AIO (IR44/45-46
        # mislabelled on those units, #293) and on non-GEN1 DC hybrids until the
        # battery-charge routing map widens (modbus #184). skip_if_none=True means
        # the sensor simply won't appear where None is the only value ever returned.
        # Not monotonic intraday (an export burst can dip it), so carries a daily
        # monotonic clamp — same contract as e_self_consumption_today.
        key="e_pv_direct_today",
        name="PV Direct Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        monotonic=True,
        # getattr, not direct access: SinglePhaseInverter-only computed field, absent on
        # the polymorphic ThreePhaseInverter — returns None there, dropped by skip_if_none.
        value_fn=lambda inv: getattr(inv, "e_pv_direct_today", None),
        skip_if_none=True,
        skip_if_ems=True,
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
        # None on AIO (no single-phase inverter-export register) — drop (#194).
        skip_if_none=True,
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
        # AIO reports a static, meaningless figure here (not None), so skip_if_none
        # never caught it — gate on AIO topology instead (#95). skip_if_none still
        # drops it on any non-AIO model where the register is genuinely absent.
        skip_if_aio=True,
        skip_if_none=True,
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
        # No PV/solar on a battery-only AIO; reads 0.0 not None, so gate on AIO (#95).
        skip_if_aio=True,
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
        # No PV strings on a battery-only AIO; reads 0 not None, so gate on AIO (#95).
        skip_if_aio=True,
        skip_if_none=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="num_phases",
        name="Phase Count",
        value_fn=lambda inv: inv.num_phases,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="grid_port_max_power_output",
        name="Grid Export Power Limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.grid_port_max_power_output,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="inverter_max_power",
        name="Inverter Max Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.inverter_max_power,
        # Computed from the device-type code; None when the DTC isn't in the
        # library's lookup (e.g. some AIO variants) — drop rather than "Unknown" (#194).
        skip_if_none=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyInverterSensorDescription(
        key="battery_max_power",
        name="Battery Max Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda inv: inv.battery_max_power,
        # Computed from DTC + firmware; None until the modbus DTC lookup has the
        # entry — drop rather than "Unknown" (#194).
        skip_if_none=True,
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
)


# Individual LV per-cell voltage/temperature entities, split out of
# BATTERY_SENSORS so they can be gated behind CONF_EXPOSE_PER_CELL. DIAGNOSTIC,
# and enabled when created (opting in IS the enable).
BATTERY_CELL_SENSORS: tuple[GivEnergyBatterySensorDescription, ...] = (
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


# All-in-One per-module roll-ups (#192), always created. The lean default when
# per-cell exposure is off: min/max/delta cell voltage + min/max cell temp,
# computed from the module's own cells — enough to spot an imbalanced or hot
# module without the 24+12 individual per-cell entities.
AIO_MODULE_SENSORS: tuple[GivEnergyAioModuleSensorDescription, ...] = (
    GivEnergyAioModuleSensorDescription(
        key="cell_voltage_min",
        name="Cell Voltage Min",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, min),
    ),
    GivEnergyAioModuleSensorDescription(
        key="cell_voltage_max",
        name="Cell Voltage Max",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, max),
    ),
    GivEnergyAioModuleSensorDescription(
        key="cell_voltage_delta",
        name="Cell Voltage Delta",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, lambda c: max(c) - min(c)),
    ),
    GivEnergyAioModuleSensorDescription(
        key="cell_temperature_min",
        name="Cell Temperature Min",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("t_cell", 12, min),
    ),
    GivEnergyAioModuleSensorDescription(
        key="cell_temperature_max",
        name="Cell Temperature Max",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("t_cell", 12, max),
    ),
)


# All-in-One per-module per-cell entities (#192), gated behind
# CONF_EXPOSE_PER_CELL. Each removable module reports its own 24 cell voltages
# and per-cell temperatures. Cell temps 13-24 read zero on known AIO hardware,
# so only 01-12 are exposed (matching what the module BMS actually populates);
# voltages cover all 24 cells.
AIO_MODULE_CELL_SENSORS: tuple[GivEnergyAioModuleSensorDescription, ...] = (
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


# All-in-One / HV battery stack (BCU) sensors (#95). The library decodes a
# cluster-level Battery Control Unit per HV stack (device 0x70+offset); these
# surface its pack-level metrics, which were previously unavailable — including
# the correct pack voltage (the inverter-level `v_battery` is a sub-100V field
# that reads Unknown on an HV stack). Power/energy go on the main device page;
# the rest are DIAGNOSTIC, matching the inverter/battery analogues.
HV_STACK_SENSORS: tuple[GivEnergyHvStackSensorDescription, ...] = (
    GivEnergyHvStackSensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_voltage"),
    ),
    GivEnergyHvStackSensorDescription(
        key="battery_current",
        name="Battery Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_current"),
    ),
    GivEnergyHvStackSensorDescription(
        key="battery_soc_max",
        name="Battery SOC Max",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_soc_max"),
    ),
    GivEnergyHvStackSensorDescription(
        key="battery_soc_min",
        name="Battery SOC Min",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_soc_min"),
    ),
    GivEnergyHvStackSensorDescription(
        key="battery_soh",
        name="Battery State of Health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_soh"),
    ),
    GivEnergyHvStackSensorDescription(
        key="charge_energy_total",
        name="Battery Charge Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_bcu_attr("charge_energy_total"),
    ),
    GivEnergyHvStackSensorDescription(
        key="discharge_energy_total",
        name="Battery Discharge Total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_bcu_attr("discharge_energy_total"),
    ),
    GivEnergyHvStackSensorDescription(
        key="charge_energy_today",
        name="Battery Charge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_bcu_attr("charge_energy_today"),
    ),
    GivEnergyHvStackSensorDescription(
        key="discharge_energy_today",
        name="Battery Discharge Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_bcu_attr("discharge_energy_today"),
    ),
    GivEnergyHvStackSensorDescription(
        key="battery_nominal_capacity_ah",
        name="Battery Nominal Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("battery_nominal_capacity_ah"),
    ),
    GivEnergyHvStackSensorDescription(
        key="remaining_battery_capacity_ah",
        name="Remaining Capacity",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("remaining_battery_capacity_ah"),
    ),
    GivEnergyHvStackSensorDescription(
        key="number_of_cycles",
        name="Charge Cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("number_of_cycles"),
    ),
    GivEnergyHvStackSensorDescription(
        key="pack_software_version",
        name="Pack Software Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bcu_attr("pack_software_version"),
    ),
)


# HV per-module (BMU) roll-ups (#179), always created. Each module in an HV stack
# decodes its own 24 cell voltages + 24 cell temperatures (givenergy-modbus 2.7.0,
# wire-confirmed). These summaries are the lean default when per-cell exposure is
# off — min/max/delta voltage + min/max temp per module.
HV_MODULE_SENSORS: tuple[GivEnergyHvModuleSensorDescription, ...] = (
    GivEnergyHvModuleSensorDescription(
        key="cell_voltage_min",
        name="Cell Voltage Min",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, min),
    ),
    GivEnergyHvModuleSensorDescription(
        key="cell_voltage_max",
        name="Cell Voltage Max",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, max),
    ),
    GivEnergyHvModuleSensorDescription(
        key="cell_voltage_delta",
        name="Cell Voltage Delta",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("v_cell", 24, lambda c: max(c) - min(c)),
    ),
    GivEnergyHvModuleSensorDescription(
        key="cell_temperature_min",
        name="Cell Temperature Min",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("t_cell", 24, min),
    ),
    GivEnergyHvModuleSensorDescription(
        key="cell_temperature_max",
        name="Cell Temperature Max",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cell_rollup("t_cell", 24, max),
    ),
)


# HV per-module per-cell entities (#179), gated behind CONF_EXPOSE_PER_CELL.
# 24 cell voltages + 24 cell temperatures per module, all wire-confirmed on the
# GIV-3HY-11 (cells ~3.30 V, temps ~36 °C). DIAGNOSTIC, enabled when created.
HV_MODULE_CELL_SENSORS: tuple[GivEnergyHvModuleSensorDescription, ...] = (
    *(
        GivEnergyHvModuleSensorDescription(
            key=f"v_cell_{i:02d}",
            name=f"Cell {i} Voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_hv_module_attr(f"v_cell_{i:02d}"),
        )
        for i in range(1, 25)
    ),
    *(
        GivEnergyHvModuleSensorDescription(
            key=f"t_cell_{i:02d}",
            name=f"Cell {i} Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_hv_module_attr(f"t_cell_{i:02d}"),
        )
        for i in range(1, 25)
    ),
)


# EMS managed-inverter summary sensors. On an EMS plant the 0x11 device is the
# controller; each inverter it manages is reported only as a blinded rollup
# summary (status/power/SoC/temp — no per-string detail). One HA device per
# managed inverter, keyed by its own serial. See GivEnergyManagedInverterSensor.
MANAGED_INVERTER_SENSORS: tuple[GivEnergyManagedInverterSensorDescription, ...] = (
    GivEnergyManagedInverterSensorDescription(
        key="status",
        name="Status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda summary: _managed_status(summary.status),
    ),
    GivEnergyManagedInverterSensorDescription(
        key="power",
        name="Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_summary_attr("p_inverter_out"),
    ),
    GivEnergyManagedInverterSensorDescription(
        key="battery_soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_summary_attr("battery_soc"),
    ),
    GivEnergyManagedInverterSensorDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary_attr("t_inverter_heatsink"),
    ),
)


def _ems_attr(name: str) -> Callable[[Ems], Any]:
    """Return a value_fn that reads `name` off the EMS plant model."""
    return lambda ems: getattr(ems, name)


# Plant-level aggregates an EMS controller carries that the inverter registers
# don't (#201) — the inverter sensors (PV/grid/battery/AC) stay on the controller;
# these complement them. Names carry an "EMS" prefix so they group on the device.
EMS_SENSORS: tuple[GivEnergyEmsSensorDescription, ...] = (
    GivEnergyEmsSensorDescription(
        key="ems_inverter_count",
        name="EMS Managed Inverter Count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("inverter_count"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyEmsSensorDescription(
        key="ems_calc_load_power",
        name="EMS Calculated Load Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("calc_load_power"),
    ),
    GivEnergyEmsSensorDescription(
        key="ems_measured_load_power",
        name="EMS Measured Load Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("measured_load_power"),
        # Reads a constant zero on current EMS firmware, where the calculated-load
        # aggregate carries the real figure. Hidden by default on new installs
        # (still recorded, like grid_power above) to declutter; existing installs
        # keep it until hidden manually — the flag only applies at registration (#52).
        entity_registry_visible_default=False,
    ),
    GivEnergyEmsSensorDescription(
        key="ems_grid_meter_power",
        name="EMS Grid Meter Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("grid_meter_power"),
    ),
    GivEnergyEmsSensorDescription(
        key="ems_total_battery_power",
        name="EMS Total Battery Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("total_battery_power"),
    ),
    GivEnergyEmsSensorDescription(
        key="ems_remaining_battery_energy",
        name="EMS Remaining Battery Energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_ems_attr("remaining_battery_wh"),
    ),
)


def _partial_failure_attributes(
    coordinator: GivEnergyUpdateCoordinator,
) -> dict[str, Any] | None:
    """Summarise the most recent partial poll's failed reads for the UI.

    Names the device(s) that dropped (e.g. "0x34" for a battery) plus the
    per-bank detail and when it last happened, so a flaky device can be
    identified even after the poll has recovered (the detail is retained past
    a clean poll — #176).
    """
    failures = coordinator.last_partial_failures
    if not failures:
        return None
    last_partial_at = coordinator.last_partial_at
    return {
        "last_failed_devices": sorted({f"0x{f.device_address:02x}" for f in failures}),
        "last_failure_count": len(failures),
        "last_failures": [
            f"0x{f.device_address:02x} "
            f"{getattr(f.request_type, 'value', f.request_type)} "
            f"@ {f.base_register}+{f.register_count}"
            for f in failures
        ],
        "last_partial_at": last_partial_at.isoformat() if last_partial_at else None,
    }


def _comms_counter_attributes(
    by_device_attr: str,
) -> Callable[[GivEnergyUpdateCoordinator], dict[str, Any] | None]:
    """Build an attributes_fn exposing a per-device breakdown of a comms counter.

    Mirrors _partial_failure_attributes: returns None when nothing has been
    counted, else a hex-keyed {device: count} map so a noisy device (a flaky
    bus, one splice-rejecting pack) can be spotted against the plant total.
    """

    def _attrs(coordinator: GivEnergyUpdateCoordinator) -> dict[str, Any] | None:
        by_device: dict[int, int] = getattr(coordinator, by_device_attr)
        if not by_device:
            return None
        return {"per_device": {f"0x{addr:02x}": count for addr, count in sorted(by_device.items())}}

    return _attrs


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
    GivEnergyCoordinatorSensorDescription(
        key="crc_failures",
        name="Comms CRC Errors",
        # Per-device CRC-failed responses the library skipped (keep-last-good).
        # A few a day is the normal noise floor; a climbing rate on one device
        # flags a degrading link. The attributes name which device(s).
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: sum(coord.crc_failures_by_device.values()),
        attributes_fn=_comms_counter_attributes("crc_failures_by_device"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="splice_rejections",
        name="Comms Splice Guard Rejections",
        # Battery banks hard-rejected by the sub-bus splice guard (keep-last-
        # good). Expected to be near-zero; a sustained climb on one pack points
        # at sub-bus corruption or a poisoned baseline.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: sum(coord.splice_rejections_by_device.values()),
        attributes_fn=_comms_counter_attributes("splice_rejections_by_device"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="splice_holds",
        name="Comms Splice Guard Holds",
        # Banks escrowed for one poll pending confirmation — the softer splice
        # signal (often benign). Counts hold events, not currently-held banks.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: sum(coord.splice_holds_by_device.values()),
        attributes_fn=_comms_counter_attributes("splice_holds_by_device"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="read_retries",
        name="Comms Read Retries",
        # Register reads that needed at least one re-request before succeeding
        # (or giving up). The earliest creeping-degradation signal — a read that
        # recovers on retry is otherwise reported as a clean success. A climbing
        # rate on one device flags a link starting to struggle.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: sum(coord.read_retries_by_device.values()),
        attributes_fn=_comms_counter_attributes("read_retries_by_device"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GivEnergyCoordinatorSensorDescription(
        key="cold_start_holds",
        name="Comms Cold Start Holds",
        # Battery banks held one extra poll at cold start awaiting baseline
        # corroboration (modbus #289). A benign "establishing baseline" signal —
        # distinct from Splice Guard Holds (which means corruption is being held).
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: sum(coord.cold_start_held_by_device.values()),
        attributes_fn=_comms_counter_attributes("cold_start_held_by_device"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def _include_inverter_sensor(
    description: GivEnergyInverterSensorDescription,
    inverter: InverterModel,
    is_three_phase: bool,
    is_aio: bool = False,
    is_ems: bool = False,
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
    if description.skip_if_aio and is_aio:
        return False
    if description.skip_if_ems and is_ems:
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
    # A pure All-in-One is battery-only, so its inverter's PV/solar fields read
    # meaningless values — gate those sensors out (#95). Key off the detected model
    # (restored from the on-disk capabilities cache), NOT decoded module telemetry:
    # an AIO whose modules drop out on the setup poll must still be recognised, and
    # ALL_IN_ONE_HYBRID genuinely has PV so is deliberately excluded.
    is_aio = bool(capabilities) and capabilities.device_type is Model.ALL_IN_ONE
    battery_data_only = entry.options.get(CONF_BATTERY_DATA_ONLY, DEFAULT_BATTERY_DATA_ONLY)
    # Whether to create the individual per-cell entities (LV pack / AIO module / HV
    # BMU). Absent ⇒ legacy install ⇒ True (keep existing per-cell entities); new
    # entries carry an explicit False from config-flow creation. See
    # CONF_EXPOSE_PER_CELL and _reconcile_per_cell_entities (__init__).
    expose_per_cell = entry.options.get(CONF_EXPOSE_PER_CELL, True)
    ems = coordinator.data.ems

    entities: list[SensorEntity] = []

    # On an EMS plant the 0x11 device is a controller, but its inverter registers
    # still carry meaningful plant data (PV/grid/battery/AC), so the inverter
    # sensors stay — only the derived House Consumption is gated out per-description
    # via skip_if_ems (#201). The EMS plant-level aggregates are added alongside.
    #
    # Battery-data-only (#95): a parallel-mode AIO is controlled by its Gateway,
    # so its own inverter-level sensors (PV/grid/load/derived consumption) are
    # misleading. Drop them; keep the battery pack / HV stack / module / coordinator
    # data below. Control platforms suppress themselves the same way.
    if not battery_data_only:
        entities.extend(
            GivEnergyInverterSensor(coordinator, description)
            for description in INVERTER_SENSORS
            if _include_inverter_sensor(
                description, inverter, is_three_phase, is_aio, ems is not None
            )
        )

    if ems is not None:
        # EMS controller plant-level aggregates the inverter registers don't carry
        # (#201): managed-inverter count, calculated/measured load, grid-meter
        # power, total battery power and remaining battery energy.
        entities.extend(GivEnergyEmsSensor(coordinator, description) for description in EMS_SENSORS)

    for battery_index, battery in enumerate(coordinator.data.batteries):
        entities.extend(
            GivEnergyBatterySensor(coordinator, description, battery_index)
            for description in BATTERY_SENSORS
        )
        if expose_per_cell:
            entities.extend(
                GivEnergyBatterySensor(coordinator, description, battery_index)
                for description in BATTERY_CELL_SENSORS
            )

    # AIO per-module battery devices (#192) — empty on non-AIO plants. A module
    # with a blank/invalid serial can't anchor a device, so skip it; the index
    # still aligns with `aio_battery_modules` for the entities we do create.
    # Like the battery entities, this set is fixed at setup: a module absent
    # during the initial probe gets no entities until a reload (tracked in #148,
    # to be fixed uniformly for all device types).
    seen_module_serials: set[str] = set()
    for module_index, module in enumerate(coordinator.data.aio_battery_modules):
        if not module.is_valid():
            continue
        # Skip a module whose serial we've already created entities for: a
        # duplicate serial (placeholder/garbled, or a repeated BMS read) would
        # otherwise collide on unique_id and HA would drop the second module's
        # entities with an error per cell (#194). Log it loudly — real modules
        # have unique serials, so a duplicate means a module is missing from HA.
        if module.serial_number in seen_module_serials:
            _LOGGER.warning(
                "Skipping AIO battery module at index %d: duplicate serial %s "
                "(real modules have unique serials — check for a placeholder/garbled read)",
                module_index,
                module.serial_number,
            )
            continue
        seen_module_serials.add(module.serial_number)
        entities.extend(
            GivEnergyAioModuleSensor(coordinator, description, module_index)
            for description in AIO_MODULE_SENSORS
        )
        if expose_per_cell:
            entities.extend(
                GivEnergyAioModuleSensor(coordinator, description, module_index)
                for description in AIO_MODULE_CELL_SENSORS
            )

    # HV battery stack (BCU) devices (#95) — empty on non-HV plants. Each stack
    # is its own device, parented to the inverter, identified by the inverter
    # serial + the stack's fixed device address (the BCU carries no serial of its
    # own). A stack whose BCU didn't decode is skipped, mirroring the AIO loop.
    for stack_index, stack in enumerate(coordinator.data.hv_stacks):
        if not stack.bcu.is_valid():
            continue
        entities.extend(
            GivEnergyHvStackSensor(coordinator, description, stack_index)
            for description in HV_STACK_SENSORS
        )
        # HV per-module (BMU) devices (#179), nested under the stack. Roll-ups are
        # always created; the 24+24 per-cell entities are gated behind
        # expose_per_cell. Dedup by serial like the AIO loop — a placeholder or
        # repeated read would otherwise collide on unique_id.
        seen_bmu_serials: set[str] = set()
        for bmu_index, bmu in enumerate(stack.bmus):
            if not bmu.is_valid():
                continue
            if bmu.serial_number in seen_bmu_serials:
                _LOGGER.warning(
                    "Skipping HV battery module at stack 0x%02x index %d: duplicate "
                    "serial %s (real modules have unique serials — check for a "
                    "placeholder/garbled read)",
                    stack.device_address,
                    bmu_index,
                    bmu.serial_number,
                )
                continue
            seen_bmu_serials.add(bmu.serial_number)
            entities.extend(
                GivEnergyHvModuleSensor(coordinator, description, stack_index, bmu_index)
                for description in HV_MODULE_SENSORS
            )
            if expose_per_cell:
                entities.extend(
                    GivEnergyHvModuleSensor(coordinator, description, stack_index, bmu_index)
                    for description in HV_MODULE_CELL_SENSORS
                )

    # EMS-managed inverters — empty unless this is an EMS plant. The 0x11 device
    # is the EMS controller; each inverter it manages is a blinded rollup summary
    # (status/power/SoC/temp). Surface each as its own device parented to the
    # controller. managed_inverters already filters empty slots; dedup by serial
    # defensively, mirroring the AIO loop.
    if ems is not None:
        seen_managed_serials: set[str] = set()
        for summary in ems.managed_inverters:
            if summary.serial_number in seen_managed_serials:
                _LOGGER.warning(
                    "Skipping EMS managed inverter: duplicate serial %s",
                    summary.serial_number,
                )
                continue
            seen_managed_serials.add(summary.serial_number)
            entities.extend(
                GivEnergyManagedInverterSensor(coordinator, description, summary.serial_number)
                for description in MANAGED_INVERTER_SENSORS
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


# Dual-role floor (in the sensor's native unit) for same-day reset detection:
# a drop must be larger than this to be considered a reset at all, AND the
# post-drop value must land within [0, ceiling] to be accepted as one. Covers
# the case where the inverter clock lags HA's local midnight by one scan
# interval, so the actual counter reset arrives on a subsequent read after the
# day boundary has already been committed. A drop to anything outside the band
# (a negative excursion, a sag to a still-large value) is transient register
# skew — e.g. a one-poll zeroed register read sinking the derived consumption
# (#142) — and must be held at the previous max, or the recorder books a fake
# reset and double-counts the recovery.
#
# The acceptance ceiling scales with time since the previous reading:
# max(floor, max-load × elapsed). At the default 30 s cadence that stays at
# the 0.5 kWh floor (tight against skew), while a long scan interval or a
# polling outage spanning the reset widens it to what a genuine post-reset
# reading can have legitimately accumulated — otherwise yesterday's max stays
# clamped and the day's statistics are lost. The trade-off is a wider
# corruptible band on the first poll after an outage; that is the price of
# not freezing the sensor for hours.
_MONOTONIC_RESET_THRESHOLD = 0.5  # kWh (matches e_consumption_today's native unit)
# Conservative continuous-load bound for the elapsed-scaled ceiling — covers
# three-phase EV-charging/heat-pump households without admitting daytime
# totals as "resets".
_MONOTONIC_RESET_MAX_LOAD_KW = 15.0

# A backing input-register bank that has stopped committing past this ceiling
# reads as a fault, not polling jitter (#152): stale-but-plausible values
# silently masking a dead bank are worse than going unavailable. Expressed as
# max(floor, scans × scan interval) so longer-interval installs scale the
# ceiling rather than false-flagging on cadence — at the 30 s default that's
# ten missed commits.
_STALE_IR_CEILING_FLOOR = 300.0  # seconds
_STALE_IR_CEILING_SCANS = 10


def _source_ir_registers(model_cls: type, key: str) -> tuple[Register, ...]:
    """The input registers backing ``key`` on ``model_cls``, () if none.

    Resolved through the model's public ``registers_of()`` accessor
    (givenergy-modbus 2.3.0, #248), so it tracks whatever layout the concrete
    model (single/three-phase, …) actually has — never a hardcoded bank list.
    Computed fields aren't in the LUT and HR-backed fields are filtered out
    (HR banks are only re-read every few ticks by design, so their age is not
    a staleness signal); both resolve to () and keep the default coordinator
    availability. Mock model classes in tests also land here, via the getattr
    fallback.
    """
    getter = getattr(model_cls, "REGISTER_GETTER", None)
    if getter is None:
        return ()
    return tuple(r for r in getter.registers_of(key) if r.reg_type == "IR")


class _StaleIRGate:
    """Marks a sensor unavailable when a backing input-register bank has stopped
    committing past a ceiling (#152).

    Shared by the inverter, battery and AIO-module sensors — each resolves its own
    device address (the inverter's is fixed, the per-pack/per-module ones come from
    capabilities or the module itself), while this mixin owns the register
    resolution, the staleness ceiling, and the per-bank age check. Freshness comes
    from the library's Plant.register_age() (2.3.0, #248), which scans the stamped
    block windows generically. A held/rejected bank stops being re-stamped, so its
    age grows past the ceiling and the device's sensors go unavailable instead of
    showing a confidently-wrong frozen value (#176).
    """

    coordinator: GivEnergyUpdateCoordinator
    _source_ir_registers: tuple[Register, ...]
    _stale_ir_ceiling: float

    def _init_stale_gate(
        self, coordinator: GivEnergyUpdateCoordinator, model_cls: type, key: str
    ) -> None:
        self._source_ir_registers = _source_ir_registers(model_cls, key)
        interval = coordinator.update_interval
        self._stale_ir_ceiling = max(
            _STALE_IR_CEILING_FLOOR,
            _STALE_IR_CEILING_SCANS * (interval.total_seconds() if interval else 0.0),
        )

    def _ir_bank_stale(self, device_address: int) -> bool:
        """True if any backing IR bank for ``device_address`` is older than the ceiling.

        False when there is no resolvable IR source (computed / HR-backed fields) or
        a bank was never committed — neither is a staleness signal.
        """
        plant = self.coordinator.data
        for register in self._source_ir_registers:
            age = plant.register_age(device_address, register)
            if age is not None and age > self._stale_ir_ceiling:
                return True
        return False


class GivEnergyInverterSensor(
    _StaleIRGate, CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity, RestoreEntity
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
        self._monotonic_last_read: datetime | None = None
        self._monotonic_reset_pending: bool | None = False
        self._monotonic_prior_day_value: float | None = None
        self._init_stale_gate(
            coordinator,
            type(coordinator.data.inverter),
            description.source_field or description.key,
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
        if not self.entity_description.monotonic_resets_daily:
            # Lifetime high-water clamp: always seed from the last persisted value,
            # regardless of date, so a post-restart dip can't re-baseline below it.
            self._monotonic_max = max(restored, 0.0)
            return
        last_date = dt_util.as_local(last_state.last_updated).date()
        if last_date == dt_util.now().date():
            # Seed the intra-day max from the last persisted value so a
            # transient dip on the first post-restart reading doesn't become
            # the new baseline while the recorder still holds the higher
            # value. Floored at zero: pre-fix versions could persist a
            # negative state (#142), which must not re-seed the baseline.
            self._monotonic_max = max(restored, 0.0)
            self._monotonic_date = last_date
        else:
            # A prior-day value is no intra-day baseline, but it is the
            # reference for the first reading of the new day: matching it
            # means the counter carried over un-reset across a restart
            # spanning midnight, so the late reset must still be admitted.
            self._monotonic_prior_day_value = max(restored, 0.0)

    @property
    def available(self) -> bool:
        """Drop to unavailable when a backing IR bank has stopped committing (#152)."""
        if not super().available:
            return False
        if not self._source_ir_registers:
            return True
        capabilities = self.coordinator.data.capabilities
        if capabilities is None:
            return True
        return not self._ir_bank_stale(capabilities.inverter_address)

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value_fn(self.coordinator.data.inverter)
        if self.entity_description.monotonic and isinstance(value, (int, float)):
            if not self.entity_description.monotonic_resets_daily:
                # Lifetime counter: pure high-water clamp. Hold the maximum and
                # never re-baseline (no daily reset to admit), so a transient skew
                # dip or a real battery-to-grid decrease can't be exposed as a
                # statistics-corrupting drop (#223). Floored at zero defensively.
                candidate = max(value, 0.0)
                if self._monotonic_max is None or candidate > self._monotonic_max:
                    self._monotonic_max = candidate
                return self._monotonic_max
            now = dt_util.now()
            today = now.date()
            last_read = self._monotonic_last_read
            self._monotonic_last_read = now
            if self._monotonic_date != today:
                # New calendar day in HA's timezone: start fresh so the
                # midnight reset passes through as a real decrease. Floored
                # at zero — register skew on a derived value can read
                # negative at any moment, including across the day boundary
                # (#142). A high carry-over here is normal (inverter clock
                # lagging HA's midnight); the reset branch below catches the
                # real reset a poll later. A reset is owed exactly when the
                # counter did NOT drop at the boundary — judged against the
                # running max, or after a restart against the restored
                # prior-day value; only while it is owed may the acceptance
                # band widen with elapsed time. An implausible (negative)
                # boundary reading decides nothing: defer the owed-reset
                # call to the first plausible reading of the day, or the
                # carry-over reappearing after the skew would lose it.
                prior_ref = (
                    self._monotonic_max
                    if self._monotonic_max is not None
                    else self._monotonic_prior_day_value
                )
                if value < 0.0:
                    self._monotonic_reset_pending = None
                    self._monotonic_prior_day_value = prior_ref
                else:
                    self._monotonic_reset_pending = (
                        prior_ref is not None and value >= prior_ref - _MONOTONIC_RESET_THRESHOLD
                    )
                    self._monotonic_prior_day_value = None
                self._monotonic_max = max(value, 0.0)
                self._monotonic_date = today
            elif self._monotonic_reset_pending is None and value >= 0.0:
                # Deferred owed-reset call (the boundary poll was skew).
                # Three outcomes: the carry-over reappearing means the reset
                # is still owed; a reading inside the (elapsed-scaled) reset
                # band means it happened during the skew window; anything in
                # between is an ambiguous still-large sag — exactly the
                # shape this clamp rejects — so it neither settles the
                # question nor gets exposed, and a later plausible poll
                # decides.
                prior_ref = self._monotonic_prior_day_value
                ceiling = _MONOTONIC_RESET_THRESHOLD
                if last_read is not None:
                    elapsed_hours = (now - last_read).total_seconds() / 3600.0
                    ceiling = max(ceiling, _MONOTONIC_RESET_MAX_LOAD_KW * elapsed_hours)
                held_max = self._monotonic_max if self._monotonic_max is not None else 0.0
                if prior_ref is not None and value >= prior_ref - _MONOTONIC_RESET_THRESHOLD:
                    self._monotonic_reset_pending = True
                    self._monotonic_prior_day_value = None
                    self._monotonic_max = max(value, held_max)
                elif prior_ref is None or value <= ceiling:
                    self._monotonic_reset_pending = False
                    self._monotonic_prior_day_value = None
                    self._monotonic_max = max(value, held_max)
                # else: ambiguous — hold the current max, stay undecided.
            elif self._monotonic_max is None:
                # Unreachable in practice (date and max are set together),
                # but keeps the invariant explicit and lets the branches
                # below assume a non-None max.
                self._monotonic_max = max(value, 0.0)
            elif value < self._monotonic_max - _MONOTONIC_RESET_THRESHOLD:
                # Large same-day drop: a genuine counter reset, but only when
                # the new value is a plausible post-reset reading (see the
                # threshold's comment). Anything else is transient register
                # skew: hold the clamp until the source recovers, so the
                # recorder never sees a fake reset followed by a
                # sum-corrupting recovery jump (#142). While the midnight
                # reset is still owed, the ceiling scales with time since
                # the previous reading so a reset observed late — long scan
                # interval, or a polling outage after the date flip — is
                # not rejected for having accumulated more than the floor.
                # Once it has been observed (or was never owed), the band
                # stays at the floor: a post-gap daytime sag is skew, not a
                # reset.
                ceiling = _MONOTONIC_RESET_THRESHOLD
                if self._monotonic_reset_pending and last_read is not None:
                    elapsed_hours = (now - last_read).total_seconds() / 3600.0
                    ceiling = max(ceiling, _MONOTONIC_RESET_MAX_LOAD_KW * elapsed_hours)
                if 0.0 <= value <= ceiling:
                    self._monotonic_max = value
                    self._monotonic_reset_pending = False
            else:
                self._monotonic_max = max(value, self._monotonic_max)
            return self._monotonic_max
        return value


class GivEnergyBatterySensor(
    _StaleIRGate, CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity
):
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
        self._init_stale_gate(coordinator, type(battery), description.key)
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
    def available(self) -> bool:
        """Drop to unavailable when this pack's IR bank has stopped committing (#152).

        Catches the library holding last-good on a sub-bus splice (#256): the bank
        stops being re-stamped and ages past the ceiling, so the pack's sensors go
        unavailable rather than showing a frozen value (the #176 case).
        """
        if not super().available:
            return False
        if not self._source_ir_registers:
            return True
        capabilities = self.coordinator.data.capabilities
        if capabilities is None or self._battery_index >= len(capabilities.lv_battery_addresses):
            return True
        return not self._ir_bank_stale(capabilities.lv_battery_addresses[self._battery_index])

    @property
    def native_value(self) -> Any:
        batteries = self.coordinator.data.batteries
        if self._battery_index >= len(batteries):
            return None
        return self.entity_description.value_fn(batteries[self._battery_index])


class GivEnergyAioModuleSensor(
    _StaleIRGate, CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity
):
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
        module = coordinator.data.aio_battery_modules[module_index]
        serial = module.serial_number
        self._module_serial = serial
        self._init_stale_gate(coordinator, type(module), description.key)
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
        # Unavailable (not cross-wired) when this module is absent from the poll,
        # then — like the inverter/battery gates (#152) — when its own IR bank has
        # stopped committing past the ceiling (a frozen/held module, #176).
        module = self._module()
        if not super().available or module is None:
            return False
        if not self._source_ir_registers:
            return True
        return not self._ir_bank_stale(module.module_address)

    @property
    def native_value(self) -> Any:
        module = self._module()
        if module is None:
            return None
        return self.entity_description.value_fn(module)


class GivEnergyHvStackSensor(
    _StaleIRGate, CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity
):
    """Pack-level sensor for one HV battery stack's BCU (#95).

    Each HV stack is its own HA device, parented to the inverter via `via_device`.
    The BCU carries no serial, so the device is identified by the inverter serial
    plus the stack's fixed device address (0x70+offset).
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyHvStackSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyHvStackSensorDescription,
        stack_index: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Bind to the stack's fixed device address, not its list position, so the
        # entity stays tied to its own stack if the list reorders or shrinks.
        stacks = coordinator.data.hv_stacks
        stack = stacks[stack_index]
        self._stack_address = stack.device_address
        self._init_stale_gate(coordinator, type(stack.bcu), description.key)
        inv_serial = coordinator.data.inverter_serial_number
        device_id = f"{inv_serial}_hvstack_{stack.device_address:#04x}"
        # Only disambiguate the device name by address when there's more than one
        # stack, so the common single-stack case stays clean.
        name = "GivEnergy HV Battery Stack"
        if len(stacks) > 1:
            name = f"{name} {stack.device_address:#04x}"
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=name,
            manufacturer="GivEnergy",
            model="HV Battery Stack (BCU)",
            sw_version=stack.bcu.pack_software_version,
            via_device=(DOMAIN, inv_serial),
        )

    def _stack(self) -> HvStack | None:
        """Resolve this entity's stack by device address in the latest data."""
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (s for s in data.hv_stacks if s.device_address == self._stack_address),
            None,
        )

    @property
    def available(self) -> bool:
        # Unavailable when this stack is absent from the poll, then — like the
        # inverter/battery/module gates (#152) — when its BCU IR bank has stopped
        # committing past the ceiling (a frozen/held stack, #176).
        stack = self._stack()
        if not super().available or stack is None:
            return False
        if not self._source_ir_registers:
            return True
        return not self._ir_bank_stale(self._stack_address)

    @property
    def native_value(self) -> Any:
        stack = self._stack()
        if stack is None:
            return None
        return self.entity_description.value_fn(stack.bcu)


class GivEnergyHvModuleSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    """Per-module sensor for one BMU in an HV battery stack (#179).

    Each module is its own HA device, nested under its HV-stack device (which is
    in turn parented to the inverter), identified by the module's serial.
    Resolved by serial each refresh — `hv_stacks[].bmus` is rebuilt per poll, so
    binding by list position would cross-wire a module to a neighbour's cell data
    if one drops out. The BMU carries no device address of its own, so
    availability is gated on the serial still being present rather than on
    IR-bank staleness (unlike the LV/AIO/stack sensors).
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyHvModuleSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyHvModuleSensorDescription,
        stack_index: int,
        bmu_index: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        stack = coordinator.data.hv_stacks[stack_index]
        bmu = stack.bmus[bmu_index]
        serial = bmu.serial_number
        self._bmu_serial = serial
        inv_serial = coordinator.data.inverter_serial_number
        stack_device_id = f"{inv_serial}_hvstack_{stack.device_address:#04x}"
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy HV Battery Module {serial}",
            manufacturer="GivEnergy",
            model="HV Battery Module",
            serial_number=serial,
            via_device=(DOMAIN, stack_device_id),
        )

    def _bmu(self) -> Bmu | None:
        """Resolve this entity's module by serial across all stacks' BMUs."""
        data = self.coordinator.data
        if data is None:
            return None
        for stack in data.hv_stacks:
            for bmu in stack.bmus:
                if bmu.serial_number == self._bmu_serial:
                    return bmu
        return None

    @property
    def available(self) -> bool:
        return super().available and self._bmu() is not None

    @property
    def native_value(self) -> Any:
        bmu = self._bmu()
        if bmu is None:
            return None
        return self.entity_description.value_fn(bmu)


class GivEnergyManagedInverterSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    """Summary sensor for one EMS-managed inverter.

    On an EMS plant the 0x11 device is the EMS *controller*; the inverters it
    manages appear only as blinded rollup summaries (status / power / SoC /
    heatsink temp — no per-string detail). Each managed inverter becomes its own
    HA device, parented to the controller via `via_device` and identified by its
    own serial namespaced with a `_managed` marker — so the rollup can't collide
    with a directly-connected inverter entry of the same serial (#203). The rollup
    lives in the EMS block on 0x11 (there's no per-inverter
    register bank), so there's no stale gate: availability is simply whether the
    serial is still present in the latest poll.
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyManagedInverterSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyManagedInverterSensorDescription,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Bind to the managed inverter's serial, not its slot position: the rollup
        # is rebuilt each refresh and a dropped slot shifts the rest, so resolving
        # by serial keeps each entity tied to its own inverter.
        self._serial = serial
        # Namespace the device + entity identity with a `_managed` marker so a rollup
        # can't collide with a directly-connected inverter entry of the same serial
        # (#203): both paths otherwise key off `(DOMAIN, serial)` / `{serial}_{key}`.
        # Serials never contain "_managed", so this is collision-proof; the real
        # serial still shows via the device name and serial_number.
        managed_id = f"{serial}_managed"
        self._attr_unique_id = f"{managed_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, managed_id)},
            name=f"GivEnergy Managed Inverter {serial}",
            manufacturer="GivEnergy",
            model="Managed Inverter (EMS)",
            serial_number=serial,
            via_device=(DOMAIN, coordinator.data.inverter_serial_number),
        )

    def _summary(self) -> InverterSummary | None:
        """Resolve this entity's managed inverter by serial in the latest data."""
        data = self.coordinator.data
        if data is None or data.ems is None:
            return None
        return next(
            (s for s in data.ems.managed_inverters if s.serial_number == self._serial),
            None,
        )

    @property
    def available(self) -> bool:
        # Unavailable when this managed inverter has dropped out of the EMS
        # rollup (its serial is no longer reported).
        return super().available and self._summary() is not None

    @property
    def native_value(self) -> Any:
        summary = self._summary()
        if summary is None:
            return None
        return self.entity_description.value_fn(summary)


class GivEnergyEmsSensor(CoordinatorEntity[GivEnergyUpdateCoordinator], SensorEntity):
    """Plant-level telemetry sensor for an EMS controller (device 0x11).

    On an EMS plant the 0x11 device is the controller, not an inverter, so its
    inverter registers (PV/battery/grid/AC) are absent and the inverter sensor set
    is suppressed (#201). This surfaces the EMS aggregates instead — status,
    managed-inverter count, load / grid / battery power and remaining energy — on
    the same controller device, which it also names (the inverter sensors that used
    to define the device are gone on EMS).
    """

    _attr_has_entity_name = True
    entity_description: GivEnergyEmsSensorDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyEmsSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
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

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.ems is not None

    @property
    def native_value(self) -> Any:
        ems = self.coordinator.data.ems
        if ems is None:
            return None
        return self.entity_description.value_fn(ems)


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
