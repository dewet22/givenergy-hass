"""GivEnergy button entities."""
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription, ButtonDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.givenergy import GivEnergyCoordinator, DOMAIN
from custom_components.givenergy.entity import InverterEntity


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the button platform."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            InverterButton(
                coordinator=coordinator,
                entity_description=ButtonEntityDescription(
                    key='inverter_reboot',
                    name='Restart',
                    icon='mdi:restart-alert',
                    device_class=ButtonDeviceClass.RESTART,
                ),
            ),
        ]
    )


class InverterButton(InverterEntity, ButtonEntity):
    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: ButtonEntityDescription):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.coordinator.data.inverter_serial_number}_{entity_description.key}"
        self._attr_name = f'Inverter {self.coordinator.data.inverter_serial_number} {entity_description.name}'
        self.entity_description = entity_description

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        return getattr(self.coordinator.inverter, self.entity_description.key)

    async def async_press(self) -> None:
        """Send command using the coordinator."""
        await self.coordinator.set_entity_state(self.entity_description.key, True)
        await self.coordinator.async_request_refresh()
