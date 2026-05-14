from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from givenergy_modbus.client import commands
from givenergy_modbus.model.inverter import BatteryPowerMode, SinglePhaseInverter
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class GivEnergySelectEntityDescription(SelectEntityDescription):
    current_option_fn: Callable[[SinglePhaseInverter], str | None] = field(default=lambda _: None)
    select_option_cmd: Callable[[str], list] = field(default=lambda _: [])


def _battery_power_mode_cmd(option: str) -> list:
    if option == "Self Consumption":
        return commands.set_discharge_mode_to_match_demand()
    return commands.set_discharge_mode_max_power()


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
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GivEnergySelectEntity(coordinator, description) for description in SELECT_DESCRIPTIONS
    )


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
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
