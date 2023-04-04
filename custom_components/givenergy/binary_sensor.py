"""Binary sensor platform for givenergy."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .const import DOMAIN, Icon
from .coordinator import GivEnergyCoordinator
from .entity import InverterEntity

ENTITY_DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="enable_charge",
        name="Enable charging",
        icon=Icon.BATTERY_PLUS,
    ),
)


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the binary_sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        InverterBinarySensor(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class InverterBinarySensor(InverterEntity, BinarySensorEntity):
    """GivEnergy inverter binary sensor class."""

    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: BinarySensorEntityDescription):
        super().__init__(coordinator)
        self._attr_unique_id = f"givenergy_inverter_{self.coordinator.data.inverter_serial_number}_{entity_description.key}"
        self.entity_description = entity_description

    @property
    def is_on(self) -> bool:
        """Return true if the binary_sensor is on."""
        return getattr(self.coordinator.inverter, self.entity_description.key)
