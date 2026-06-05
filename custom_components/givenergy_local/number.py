from __future__ import annotations

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

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel
from .sensor import _device_kind


@dataclass(frozen=True, kw_only=True)
class GivEnergyNumberEntityDescription(NumberEntityDescription):
    value_fn: Callable[[InverterModel], float | None] = field(default=lambda _: None)
    set_value_cmd: Callable[[float], list] = field(default=lambda _: [])


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
        set_value_cmd=lambda v: commands.set_charge_target(int(v)),
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
        native_max_value=50,
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
        native_max_value=50,
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
        set_value_cmd=lambda v: commands.set_active_power_rate(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- AC-config-block controls (AC-coupled inverters + single-phase All-in-One) ---

# The AC charge/discharge power limits (HR313/314) are distinct from the DC-side
# limits above (HR111/112) and are only meaningful on models that expose the
# AC-config register block (HR300+). Gated via PlantCapabilities.has_ac_config_block
# (and not is_three_phase — three-phase AC remaps the read-back to different registers
# than the command writes; see modbus#75). The setter rejects values below 1, hence the
# 1–100 range.
AC_COUPLED_NUMBER_DESCRIPTIONS: tuple[GivEnergyNumberEntityDescription, ...] = (
    GivEnergyNumberEntityDescription(
        key="battery_charge_limit_ac",
        name="Battery AC Charge Limit",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_charge_limit_ac,
        set_value_cmd=lambda v: commands.set_battery_charge_limit_ac(int(v)),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyNumberEntityDescription(
        key="battery_discharge_limit_ac",
        name="Battery AC Discharge Limit",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda inv: inv.battery_discharge_limit_ac,
        set_value_cmd=lambda v: commands.set_battery_discharge_limit_ac(int(v)),
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        GivEnergyNumberEntity(coordinator, description) for description in NUMBER_DESCRIPTIONS
    ]
    if coordinator.data.ems is not None:
        entities.extend(
            GivEnergyEmsNumberEntity(coordinator, description)
            for description in EMS_NUMBER_DESCRIPTIONS
        )
    caps = coordinator.data.capabilities
    if caps is not None and caps.has_ac_config_block and not caps.is_three_phase:
        entities.extend(
            GivEnergyNumberEntity(coordinator, description)
            for description in AC_COUPLED_NUMBER_DESCRIPTIONS
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
