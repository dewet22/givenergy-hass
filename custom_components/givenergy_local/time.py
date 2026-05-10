from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time
from collections.abc import Callable
from typing import Any

from givenergy_modbus.client import commands
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.inverter import Inverter
from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class GivEnergyTimeEntityDescription(TimeEntityDescription):
    slot_fn: Callable[[Inverter], TimeSlot | None] = field(default=lambda _: None)
    is_start: bool = True
    set_slot_cmd: Callable[[TimeSlot], list] = field(default=lambda _: [])


TIME_DESCRIPTIONS: tuple[GivEnergyTimeEntityDescription, ...] = (
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_start",
        name="Charge Slot 1 Start",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=True,
        set_slot_cmd=lambda ts: commands.set_charge_slot_1(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_end",
        name="Charge Slot 1 End",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=False,
        set_slot_cmd=lambda ts: commands.set_charge_slot_1(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_start",
        name="Charge Slot 2 Start",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=True,
        set_slot_cmd=lambda ts: commands.set_charge_slot_2(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_end",
        name="Charge Slot 2 End",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=False,
        set_slot_cmd=lambda ts: commands.set_charge_slot_2(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_start",
        name="Discharge Slot 1 Start",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=True,
        set_slot_cmd=lambda ts: commands.set_discharge_slot_1(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_end",
        name="Discharge Slot 1 End",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=False,
        set_slot_cmd=lambda ts: commands.set_discharge_slot_1(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_start",
        name="Discharge Slot 2 Start",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=True,
        set_slot_cmd=lambda ts: commands.set_discharge_slot_2(ts),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_end",
        name="Discharge Slot 2 End",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=False,
        set_slot_cmd=lambda ts: commands.set_discharge_slot_2(ts),
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
        GivEnergyTimeEntity(coordinator, description)
        for description in TIME_DESCRIPTIONS
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
        current_slot = self.entity_description.slot_fn(self.coordinator.data.inverter)
        if current_slot is None:
            current_slot = TimeSlot(start=dt_time(0, 0), end=dt_time(0, 0))
        if self.entity_description.is_start:
            new_slot = TimeSlot(start=value, end=current_slot.end)
        else:
            new_slot = TimeSlot(start=current_slot.start, end=value)
        await client.one_shot_command(self.entity_description.set_slot_cmd(new_slot))
        await self.coordinator.async_request_refresh()
