from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from givenergy_modbus.client import commands
from givenergy_modbus.model.ems import Ems
from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BATTERY_DATA_ONLY, DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel
from .sensor import _device_kind

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GivEnergyNumberEntityDescription(NumberEntityDescription):
    value_fn: Callable[[InverterModel], float | None] = field(default=lambda _: None)
    set_value_cmd: Callable[[float], list] = field(default=lambda _: [])
    # If True, the control isn't created when value_fn reads None at setup — the
    # register isn't present on this device/firmware (e.g. HR313/314 on older
    # firmware). The control-side of the sensor skip_if_none (#207).
    skip_if_none: bool = False


NUMBER_DESCRIPTIONS: tuple[GivEnergyNumberEntityDescription, ...] = (
    GivEnergyNumberEntityDescription(
        key="charge_target_soc",
        name="Charge Target SOC",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=4,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.charge_target_soc,
        set_value_cmd=lambda v: commands.set_charge_target_enabled(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_soc_reserve",
        name="Battery SOC Reserve",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=4,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_soc_reserve,
        set_value_cmd=lambda v: commands.set_battery_soc_reserve(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_charge_limit",
        name="Battery Charge Limit",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_charge_limit,
        set_value_cmd=lambda v: commands.set_battery_charge_limit(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_discharge_limit",
        name="Battery Discharge Limit",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_discharge_limit,
        set_value_cmd=lambda v: commands.set_battery_discharge_limit(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_discharge_min_power_reserve",
        name="Battery Discharge Min Power Reserve",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=4,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_discharge_min_power_reserve,
        set_value_cmd=lambda v: commands.set_battery_power_reserve(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="active_power_rate",
        name="Inverter Max Output Active Power",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.active_power_rate,
        # On an EMS plant this HR50 write is a silent no-op: the controller governs
        # each managed inverter's active power and re-asserts it, so a per-inverter
        # write is accepted but overridden (modbus #304, hardware-confirmed #218).
        # No EMS-controller active-power command exists to route to, and a per-inverter
        # config entry can't tell it's EMS-managed — so this is left as-is on EMS.
        set_value_cmd=lambda v: commands.set_active_power_rate(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- AC-config-block controls (AC-coupled inverters + single-phase All-in-One) ---

# DC battery power-limit controls (HR111/112) suppressed on plants that expose the
# AC-config block — there the AC pair (HR313/314) is the battery-power control and the
# DC pair targets a different register (modbus #301/#302).
_DC_BATTERY_LIMIT_KEYS = frozenset({"battery_charge_limit", "battery_discharge_limit"})

# The AC charge/discharge power limits (HR313/314) are distinct from the DC-side
# limits above (HR111/112) and are only meaningful on models that expose the
# AC-config register block (HR300+). Gated via PlantCapabilities.has_ac_config_block
# (and not is_three_phase — three-phase AC remaps the read-back to different registers
# than the command writes; see modbus#75). Range is 1–100%, unlike the DC pair's
# 0–100: HR313/314 ERROR on a 0 write (hardware-confirmed on #52 — AC-specific, the
# DC HR111/112 pair does accept 0), so the AC floor is 1 (≈near-zero power).
AC_COUPLED_NUMBER_DESCRIPTIONS: tuple[GivEnergyNumberEntityDescription, ...] = (
    GivEnergyNumberEntityDescription(
        key="battery_charge_limit_ac",
        name="Inverter Charge Power Percentage",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_charge_limit_ac,
        set_value_cmd=lambda v: commands.set_battery_charge_limit_ac(int(v)),
        skip_if_none=True,  # HR313 absent on older firmware (#207)
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_discharge_limit_ac",
        name="Inverter Discharge Power Percentage",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_discharge_limit_ac,
        set_value_cmd=lambda v: commands.set_battery_discharge_limit_ac(int(v)),
        skip_if_none=True,  # HR314 absent on older firmware (#207)
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- EMS plant-level per-slot SoC targets (only created for EMS plants) ---


@dataclass(frozen=True, kw_only=True)
class GivEnergyEmsNumberEntityDescription(NumberEntityDescription):
    value_fn: Callable[[Ems], float | None] = field(default=lambda _: None)
    set_value_cmd: Callable[[float], list] = field(default=lambda _: [])


def _target_getter(attr: str) -> Callable[[Ems], float | None]:
    return lambda ems: getattr(ems, attr)


def _target_setter(cmd: Callable[[int, int], list], idx: int) -> Callable[[float], list]:
    return lambda v: cmd(idx, int(v))


def _ems_number_descriptions() -> tuple[GivEnergyEmsNumberEntityDescription, ...]:
    """Per-slot SoC target controls for EMS charge, discharge & export slots 1-3."""
    descriptions: list[GivEnergyEmsNumberEntityDescription] = []
    for kind in ("charge", "discharge", "export"):
        cmd = getattr(commands, f"set_ems_{kind}_target_soc")
        for idx in (1, 2, 3):
            descriptions.append(
                GivEnergyEmsNumberEntityDescription(
                    key=f"ems_{kind}_target_soc_{idx}",
                    name=f"EMS {kind.title()} Slot {idx} Target SOC",
                    native_unit_of_measurement=PERCENTAGE,
                    native_min_value=4,
                    native_max_value=100,
                    native_step=1,
                    mode=NumberMode.BOX,
                    value_fn=_target_getter(f"{kind}_target_{idx}"),
                    set_value_cmd=_target_setter(cmd, idx),
                    entity_category=EntityCategory.CONFIG,
                )
            )
    return tuple(descriptions)


EMS_NUMBER_DESCRIPTIONS: tuple[GivEnergyEmsNumberEntityDescription, ...] = (
    *_ems_number_descriptions(),
    GivEnergyEmsNumberEntityDescription(
        key="ems_export_power_limit",
        name="EMS Export Power Limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        # Provisional ceiling — the raw register is uint16 (0-65535 W), which is no
        # sensible slider. 6000 W covers a single inverter's rating; confirm a realistic
        # plant-level figure with a real EMS plant (#52) and raise if needed.
        native_max_value=6000,
        native_step=100,
        mode=NumberMode.BOX,
        value_fn=lambda ems: ems.export_power_limit,
        set_value_cmd=lambda v: commands.set_ems_export_power_limit(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
)


def _include_number(
    description: GivEnergyNumberEntityDescription, inverter: InverterModel, clean: bool = True
) -> bool:
    """Whether to create a number control at setup (#207).

    A skip_if_none control is dropped when its register reads None — absent on this
    device/firmware (e.g. HR313/314 on older firmware). Guarded so a value_fn that
    raises (a field renamed in givenergy-modbus) skips just that control, not the
    whole platform.
    """
    if not description.skip_if_none:
        return True
    # On a partial seed poll a None may be a transient bank failure, not structural
    # absence — keep the control so a later clean poll recovers it (#208 review).
    if not clean:
        return True
    try:
        value = description.value_fn(inverter)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Skipping number %s: value_fn raised at setup", description.key)
        return False
    if value is None:
        # Expected: the register isn't present on this device/firmware. DEBUG, not
        # WARNING — this is the gate working as designed and recurs every restart,
        # unlike the duplicate/garbled anomalies that warrant a warning.
        _LOGGER.debug("Skipping number %s: register reads None at setup", description.key)
        return False
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.options.get(CONF_BATTERY_DATA_ONLY, False):
        # Battery-data-only (#95): this unit's controls are owned by its Gateway.
        return
    entities: list[NumberEntity] = []
    if coordinator.data.ems is not None:
        # EMS plant: the controller's slot/target controls are authoritative; all
        # inverter-level controls (standard + AC-coupled) are redundant here (#201).
        entities.extend(
            GivEnergyEmsNumberEntity(coordinator, description)
            for description in EMS_NUMBER_DESCRIPTIONS
        )
    else:
        caps = coordinator.data.capabilities
        ac_battery_control = (
            caps is not None and caps.has_ac_config_block and not caps.is_three_phase
        )
        descriptions: tuple[GivEnergyNumberEntityDescription, ...] = NUMBER_DESCRIPTIONS
        if ac_battery_control:
            # On AC-coupled / AIO plants battery power is controlled via the AC pair
            # (HR313/314) created below; the DC pair (HR111/112) targets a different
            # register here and would mislead, so suppress it (modbus #301/#302).
            descriptions = tuple(
                d for d in NUMBER_DESCRIPTIONS if d.key not in _DC_BATTERY_LIMIT_KEYS
            )
        entities.extend(
            GivEnergyNumberEntity(coordinator, description) for description in descriptions
        )
        if ac_battery_control:
            inverter = coordinator.data.inverter
            clean = not coordinator.last_partial_failures
            entities.extend(
                GivEnergyNumberEntity(coordinator, description)
                for description in AC_COUPLED_NUMBER_DESCRIPTIONS
                if _include_number(description, inverter, clean)
            )
    async_add_entities(entities)


class GivEnergyNumberEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], NumberEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergyNumberEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.data.inverter)

    async def async_set_native_value(self, value: float) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        await client.one_shot_command(self.entity_description.set_value_cmd(value))
        await self.coordinator.async_request_refresh()


class GivEnergyEmsNumberEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], NumberEntity):
    """SoC target control for an EMS plant-level charge/discharge slot."""

    _attr_has_entity_name = True
    entity_description: GivEnergyEmsNumberEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyEmsNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )

    @property
    def native_value(self) -> float | None:
        ems = self.coordinator.data.ems
        if ems is None:
            return None
        return self.entity_description.value_fn(ems)

    async def async_set_native_value(self, value: float) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        await client.one_shot_command(self.entity_description.set_value_cmd(value))
        await self.coordinator.async_request_refresh()
