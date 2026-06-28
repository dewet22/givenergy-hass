from __future__ import annotations

import logging

from givenergy_modbus.client import commands
from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BATTERY_DATA_ONLY, DOMAIN
from .coordinator import GivEnergyUpdateCoordinator
from .sensor import _device_kind

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [
        # Re-detect is a safe read-side reload (clears the cached topology and
        # re-probes), so it's offered on every entry — including the EMS controller,
        # which otherwise exposes no controls (#233/#234). It's the in-UI equivalent
        # of the redetect_plant service, for recovering a battery/device that went
        # offline and came back.
        GivEnergyRedetectButton(coordinator, entry.entry_id),
    ]
    # Restart writes the inverter reboot register (HR163), which an EMS controller
    # rejects (not in its modbus write-safe set — same family as the clock-write
    # ban), so gate it off EMS like the other controls. Battery-data-only units are
    # driven by their Gateway, so skip there too (#95).
    if coordinator.data.ems is None and not entry.options.get(CONF_BATTERY_DATA_ONLY, False):
        entities.append(GivEnergyRestartButton(coordinator))
    async_add_entities(entities)


class _GivEnergyButtonBase(CoordinatorEntity[GivEnergyUpdateCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GivEnergyUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )


class GivEnergyRestartButton(_GivEnergyButtonBase):
    """Reboot the inverter (HR163) — the button form of the reboot_inverter service."""

    _attr_name = "Restart"
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator: GivEnergyUpdateCoordinator) -> None:
        super().__init__(coordinator, "restart")

    async def async_press(self) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            raise HomeAssistantError("GivEnergy inverter is not currently connected")
        # No refresh: the inverter drops off the bus as it reboots; the coordinator
        # reconnects on its own. A request_refresh here would just race the reboot.
        await client.one_shot_command(commands.set_inverter_reboot())


class GivEnergyRedetectButton(_GivEnergyButtonBase):
    """Clear the cached plant topology and reload — re-probe for added/recovered devices."""

    _attr_name = "Re-detect Plant"

    def __init__(self, coordinator: GivEnergyUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, "redetect_plant")
        self._entry_id = entry_id

    async def async_press(self) -> None:
        # Lazy import to avoid a package import cycle (mirrors repairs.py).
        from . import _redetect_plant_entry

        await _redetect_plant_entry(self.hass, self._entry_id)
