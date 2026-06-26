from __future__ import annotations

import logging
from datetime import datetime

from givenergy_modbus.client import commands
from homeassistant.components.datetime import DateTimeEntity, DateTimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_BATTERY_DATA_ONLY, DOMAIN
from .coordinator import GivEnergyUpdateCoordinator
from .sensor import _device_kind

_LOGGER = logging.getLogger(__name__)

SYSTEM_TIME_DESCRIPTION = DateTimeEntityDescription(
    key="system_time",
    name="System Time",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.options.get(CONF_BATTERY_DATA_ONLY, False):
        # Battery-data-only (#95): this unit's controls are owned by its Gateway.
        return
    # The inverter clock (HR35-40) is present on both directly-connected inverters
    # and EMS controllers, so create it unconditionally — but skip if the register
    # reads None on a clean poll (absent on this device/firmware). A partial seed
    # may read None transiently, so keep it then and let a later poll recover (#219).
    clean = not coordinator.last_partial_failures
    if clean and coordinator.data.inverter.system_time is None:
        _LOGGER.debug("Skipping System Time: register reads None at setup")
        return
    async_add_entities([GivEnergyDateTimeEntity(coordinator, SYSTEM_TIME_DESCRIPTION)])


class GivEnergyDateTimeEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], DateTimeEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: DateTimeEntityDescription,
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
    def native_value(self) -> datetime | None:
        # The inverter reports a naive local wall-clock time; HA's DateTimeEntity
        # requires a timezone-aware value, so interpret it in HA's configured zone
        # (the zone its clock is synced from). None on a bad poll -> unavailable.
        system_time = self.coordinator.data.inverter.system_time
        if system_time is None:
            return None
        return system_time.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    async def async_set_value(self, value: datetime) -> None:
        # On an EMS plant the clock register is read-only to us: the modbus library
        # models the controller as a non-inverter peer and refuses HR(35) writes
        # (the controller re-syncs its clock from the GivEnergy cloud). The entity
        # stays for visibility, but raise a clean error rather than letting the
        # library's InvalidPduState surface as a traceback.
        if self.coordinator.data.ems is not None:
            raise HomeAssistantError(
                "The EMS controller clock cannot be set locally — correct it from "
                "the GivEnergy app or portal."
            )
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        # HA passes a timezone-aware datetime; the inverter clock is local wall-clock,
        # matching what the set_system_datetime service writes (it passes dt_util.now()).
        await client.one_shot_command(commands.set_system_date_time(dt_util.as_local(value)))
        await self.coordinator.async_request_refresh()
