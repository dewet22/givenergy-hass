from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from givenergy_modbus.client import commands
from givenergy_modbus.model.battery import BatteryPauseMode, ExportPriority
from givenergy_modbus.model.inverter import BatteryPowerMode
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel
from .sensor import _device_kind


@dataclass(frozen=True, kw_only=True)
class GivEnergySelectEntityDescription(SelectEntityDescription):
    current_option_fn: Callable[[InverterModel], str | None] = field(default=lambda _: None)
    select_option_cmd: Callable[[str], list] = field(default=lambda _: [])


def _battery_power_mode_cmd(option: str) -> list:
    if option == "Self Consumption":
        return commands.set_discharge_mode_to_match_demand()
    return commands.set_discharge_mode_max_power()


# Human-readable labels derived from BatteryPauseMode enum names. Kept as a
# bidirectional mapping so the select option and the command stay in sync.
_PAUSE_MODE_LABELS: dict[BatteryPauseMode, str] = {
    BatteryPauseMode.DISABLED: "Disabled",
    BatteryPauseMode.PAUSE_CHARGE: "Pause Charge",
    BatteryPauseMode.PAUSE_DISCHARGE: "Pause Discharge",
    BatteryPauseMode.PAUSE_BOTH: "Pause Both",
}
_PAUSE_MODE_BY_LABEL: dict[str, BatteryPauseMode] = {v: k for k, v in _PAUSE_MODE_LABELS.items()}


def _battery_pause_current_option(inv: InverterModel) -> str | None:
    if inv.battery_pause_mode is None:
        return None
    return _PAUSE_MODE_LABELS.get(BatteryPauseMode(inv.battery_pause_mode))


def _battery_pause_mode_cmd(option: str) -> list:
    return commands.set_battery_pause_mode(_PAUSE_MODE_BY_LABEL[option])


SELECT_DESCRIPTIONS: tuple[GivEnergySelectEntityDescription, ...] = (
    GivEnergySelectEntityDescription(
        key="battery_power_mode",
        name="Battery Power Mode",
        options=["Export", "Self Consumption"],
        current_option_fn=lambda inv: (
            "Self Consumption"
            if inv.battery_power_mode == BatteryPowerMode.SELF_CONSUMPTION
            else "Export"
        ),
        select_option_cmd=_battery_power_mode_cmd,
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergySelectEntityDescription(
        key="battery_pause_mode",
        name="Battery Pause Mode",
        options=list(_PAUSE_MODE_BY_LABEL.keys()),
        current_option_fn=_battery_pause_current_option,
        select_option_cmd=_battery_pause_mode_cmd,
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- AC-config-block select controls (AC-coupled inverters + single-phase All-in-One) ---

# Export priority (HR311): only meaningful on models exposing the AC-config block;
# three-phase AC excluded pending per-model register work (modbus#75).
_EXPORT_PRIORITY_LABELS: dict[ExportPriority, str] = {
    ExportPriority.BATTERY_FIRST: "Battery First",
    ExportPriority.GRID_FIRST: "Grid First",
    ExportPriority.LOAD_FIRST: "Load First",
}
_EXPORT_PRIORITY_BY_LABEL: dict[str, ExportPriority] = {
    v: k for k, v in _EXPORT_PRIORITY_LABELS.items()
}


def _export_priority_current_option(inv: InverterModel) -> str | None:
    if inv.export_priority is None:
        return None
    return _EXPORT_PRIORITY_LABELS.get(ExportPriority(inv.export_priority))


AC_COUPLED_SELECT_DESCRIPTIONS: tuple[GivEnergySelectEntityDescription, ...] = (
    GivEnergySelectEntityDescription(
        key="export_priority",
        name="Export Priority",
        options=list(_EXPORT_PRIORITY_BY_LABEL.keys()),
        current_option_fn=_export_priority_current_option,
        select_option_cmd=lambda option: commands.set_export_priority(
            _EXPORT_PRIORITY_BY_LABEL[option]
        ),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [
        GivEnergySelectEntity(coordinator, description) for description in SELECT_DESCRIPTIONS
    ]
    caps = coordinator.data.capabilities
    if caps is not None and caps.has_ac_config_block and not caps.is_three_phase:
        entities.extend(
            GivEnergySelectEntity(coordinator, description)
            for description in AC_COUPLED_SELECT_DESCRIPTIONS
        )
    async_add_entities(entities)


class GivEnergySelectEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], SelectEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergySelectEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergySelectEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_options = list(description.options)
        # Carry the device name (mirroring sensor.py/binary_sensor.py) so HA derives
        # the device-name-prefixed entity_id slug even when the select platform sets
        # up before the sensor platform has registered the named device record.
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )

    @property
    def current_option(self) -> str | None:
        return self.entity_description.current_option_fn(self.coordinator.data.inverter)

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        await client.one_shot_command(self.entity_description.select_option_cmd(option))
        await self.coordinator.async_request_refresh()
