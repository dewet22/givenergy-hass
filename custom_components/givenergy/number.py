"""Number platform for GivEnergy."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import DEVICE_DEFAULT_NAME, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import DOMAIN, GivEnergyCoordinator
from .entity import InverterEntity

ENTITY_DESCRIPTIONS = (
    NumberEntityDescription(
        key="battery_soc_reserve",
        name="Battery SOC reserve",
        # icon=,
        # device_class=NumberDeviceClass.BATTERY,
        # mode=NumberMode.SLIDER,
        native_min_value=4,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement='%',
    ),
    NumberEntityDescription(
        key="battery_discharge_min_power_reserve",
        name="Battery discharge minimum power reserve",
        # icon=,
        # device_class=NumberDeviceClass.BATTERY,
        # mode=NumberMode.SLIDER,
        # native_min_value=4,
        # native_max_value=100,
        native_step=1,
        native_unit_of_measurement='%',
    ),
    NumberEntityDescription(
        key="battery_charge_limit",
        name="Battery charge limit",
        # icon=,
        # device_class=NumberDeviceClass.BATTERY,
        # mode=NumberMode.SLIDER,
        native_min_value=0,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement='%',
    ),
    NumberEntityDescription(
        key="battery_discharge_limit",
        name="Battery discharge limit",
        # icon=,
        # device_class=NumberDeviceClass.BATTERY,
        # mode=NumberMode.SLIDER,
        native_min_value=0,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement='%',
    ),
)


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the demo Number entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        InverterNumber(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class InverterNumber(InverterEntity, NumberEntity):
    """Representation of an inverter Number entity."""

    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: NumberEntityDescription):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.coordinator.data.inverter_serial_number}_{entity_description.key}"
        self._attr_name = f'Inverter {self.coordinator.data.inverter_serial_number} {entity_description.name}'
        self.entity_description = entity_description

    @property
    def native_value(self) -> str:
        """Return the native value of the number."""
        return getattr(self.coordinator.inverter, self.entity_description.key)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self.coordinator.set_entity_state(self.entity_description.key, value)
        await self.coordinator.async_request_refresh()
