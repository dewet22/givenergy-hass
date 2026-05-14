from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from givenergy_modbus.client import commands
from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel


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
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GivEnergyNumberEntity(coordinator, description) for description in NUMBER_DESCRIPTIONS
    )


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
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
