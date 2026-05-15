from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time as dt_time

from givenergy_modbus.client import commands
from givenergy_modbus.model import TimeSlot
from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel


@dataclass(frozen=True, kw_only=True)
class GivEnergyTimeEntityDescription(TimeEntityDescription):
    slot_fn: Callable[[InverterModel], TimeSlot | None] = field(default=lambda _: None)
    is_start: bool = True
    # Writes just the relevant endpoint register. Takes the new value and the
    # current inverter — charge/discharge setters dispatch on inverter.slot_map
    # to handle extended-slot models; the pause setter ignores the inverter.
    setter_fn: Callable[[dt_time, InverterModel], list] = field(default=lambda _, __: [])


TIME_DESCRIPTIONS: tuple[GivEnergyTimeEntityDescription, ...] = (
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_start",
        name="Charge Slot 1 Start",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_charge_slot_start(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_end",
        name="Charge Slot 1 End",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_charge_slot_end(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_start",
        name="Charge Slot 2 Start",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_charge_slot_start(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_end",
        name="Charge Slot 2 End",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_charge_slot_end(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_start",
        name="Discharge Slot 1 Start",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_discharge_slot_start(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_end",
        name="Discharge Slot 1 End",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_discharge_slot_end(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_start",
        name="Discharge Slot 2 Start",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_discharge_slot_start(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_end",
        name="Discharge Slot 2 End",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_discharge_slot_end(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="battery_pause_slot_start",
        name="Battery Pause Slot Start",
        slot_fn=lambda inv: inv.battery_pause_slot_1,
        is_start=True,
        setter_fn=lambda value, _inv: commands.set_pause_slot_start(value),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="battery_pause_slot_end",
        name="Battery Pause Slot End",
        slot_fn=lambda inv: inv.battery_pause_slot_1,
        is_start=False,
        setter_fn=lambda value, _inv: commands.set_pause_slot_end(value),
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
        GivEnergyTimeEntity(coordinator, description) for description in TIME_DESCRIPTIONS
    )


class GivEnergyTimeEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], TimeEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergyTimeEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyTimeEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
        )

    @property
    def native_value(self) -> dt_time | None:
        slot = self.entity_description.slot_fn(self.coordinator.data.inverter)
        if slot is None:
            return None
        return slot.start if self.entity_description.is_start else slot.end

    async def async_set_value(self, value: dt_time) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        inverter = self.coordinator.data.inverter
        await client.one_shot_command(self.entity_description.setter_fn(value, inverter))
        await self.coordinator.async_request_refresh()
