from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from givenergy_modbus.client import commands
from givenergy_modbus.model.ems import Ems
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel


@dataclass(frozen=True, kw_only=True)
class GivEnergySwitchEntityDescription(SwitchEntityDescription):
    is_on_fn: Callable[[InverterModel], bool] = field(default=lambda _: False)
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
    GivEnergySwitchEntityDescription(
        key="enable_rtc",
        name="Real Time Control",
        is_on_fn=lambda inv: inv.enable_rtc,
        turn_on_cmd=lambda: commands.set_enable_rtc(True),
        turn_off_cmd=lambda: commands.set_enable_rtc(False),
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- AC-config-block switches (AC-coupled inverters + single-phase All-in-One) ---

# EPS (HR317): only meaningful on models exposing the AC-config block; three-phase AC
# excluded pending per-model register work (modbus#75).
AC_COUPLED_SWITCH_DESCRIPTIONS: tuple[GivEnergySwitchEntityDescription, ...] = (
    GivEnergySwitchEntityDescription(
        key="enable_eps",
        name="Emergency Power Supply (EPS)",
        is_on_fn=lambda inv: inv.enable_eps,
        turn_on_cmd=lambda: commands.set_enable_eps(True),
        turn_off_cmd=lambda: commands.set_enable_eps(False),
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- EMS plant-level switches (only created for EMS plants) ---


@dataclass(frozen=True, kw_only=True)
class GivEnergyEmsSwitchEntityDescription(SwitchEntityDescription):
    is_on_fn: Callable[[Ems], bool | None] = field(default=lambda _: None)
    turn_on_cmd: Callable[[], list] = field(default=list)
    turn_off_cmd: Callable[[], list] = field(default=list)


EMS_SWITCH_DESCRIPTIONS: tuple[GivEnergyEmsSwitchEntityDescription, ...] = (
    GivEnergyEmsSwitchEntityDescription(
        key="ems_plant_enable",
        name="Flexi EMS Control",
        is_on_fn=lambda ems: ems.plant_enabled,
        turn_on_cmd=lambda: commands.set_ems_plant(True),
        turn_off_cmd=lambda: commands.set_ems_plant(False),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        GivEnergySwitchEntity(coordinator, description) for description in SWITCH_DESCRIPTIONS
    ]
    caps = coordinator.data.capabilities
    if caps is not None and caps.has_ac_config_block and not caps.is_three_phase:
        entities.extend(
            GivEnergySwitchEntity(coordinator, description)
            for description in AC_COUPLED_SWITCH_DESCRIPTIONS
        )
    if coordinator.data.ems is not None:
        entities.extend(
            GivEnergyEmsSwitchEntity(coordinator, description)
            for description in EMS_SWITCH_DESCRIPTIONS
        )
    async_add_entities(entities)


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


class GivEnergyEmsSwitchEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], SwitchEntity):
    """Plant-level EMS switch (e.g. the Flexi EMS Control master enable)."""

    _attr_has_entity_name = True
    entity_description: GivEnergyEmsSwitchEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyEmsSwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
        )

    @property
    def is_on(self) -> bool | None:
        ems = self.coordinator.data.ems
        if ems is None:
            return None
        return self.entity_description.is_on_fn(ems)

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
