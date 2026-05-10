from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Awaitable
from typing import Any

from givenergy_modbus.client import commands
from givenergy_modbus.model.inverter import Inverter
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class GivEnergySwitchEntityDescription(SwitchEntityDescription):
    is_on_fn: Callable[[Inverter], bool] = field(default=lambda _: False)
    turn_on_cmd: Callable[[], list] = field(default=list)
    turn_off_cmd: Callable[[], list] = field(default=list)


SWITCH_DESCRIPTIONS: tuple[GivEnergySwitchEntityDescription, ...] = (
    GivEnergySwitchEntityDescription(
        key="enable_charge",
        name="Enable Charge",
        is_on_fn=lambda inv: inv.enable_charge,
        turn_on_cmd=lambda: commands.set_enable_charge(True),
        turn_off_cmd=lambda: commands.set_enable_charge(False),
    ),
    GivEnergySwitchEntityDescription(
        key="enable_discharge",
        name="Enable Discharge",
        is_on_fn=lambda inv: inv.enable_discharge,
        turn_on_cmd=lambda: commands.set_enable_discharge(True),
        turn_off_cmd=lambda: commands.set_enable_discharge(False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GivEnergySwitchEntity(coordinator, description)
        for description in SWITCH_DESCRIPTIONS
    )


class GivEnergySwitchEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergySwitchEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergySwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
        )

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data.inverter)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send_command(self.entity_description.turn_on_cmd())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_command(self.entity_description.turn_off_cmd())

    async def _send_command(self, cmd: list) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        await client.one_shot_command(cmd)
        await self.coordinator.async_request_refresh()
