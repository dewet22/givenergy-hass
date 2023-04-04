"""Switch platform for GivEnergy."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription

from .const import DOMAIN, Icon
from .coordinator import GivEnergyCoordinator
from .entity import InverterEntity

ENTITY_DESCRIPTIONS = (
    SwitchEntityDescription(
        key="enable_charge",
        name="Enable timed charge",
        icon=Icon.BATTERY_PLUS,
    ),
    SwitchEntityDescription(
        key="enable_charge_target",
        name="Enable timed charge SOC target",
        icon=Icon.BATTERY_PLUS,
    ),
    SwitchEntityDescription(
        key="enable_discharge",
        name="Enable timed discharge",
        icon=Icon.BATTERY_MINUS,
    ),
)


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        InverterSwitch(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class InverterSwitch(InverterEntity, SwitchEntity):
    """GivEnergy switch class."""

    def __init__(self, coordinator: GivEnergyCoordinator, entity_description: SwitchEntityDescription) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.coordinator.data.inverter_serial_number}_{entity_description.key}"
        self._attr_name = f'Inverter {self.coordinator.data.inverter_serial_number} {entity_description.name}'
        self.entity_description = entity_description

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return getattr(self.coordinator.inverter, self.entity_description.key)

    async def _set_entity_state(self, value: bool):
        await self.coordinator.set_entity_state(self.entity_description.key, value)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **_: any) -> None:
        """Turn on the switch."""
        await self._set_entity_state(True)

    async def async_turn_off(self, **_: any) -> None:
        """Turn off the switch."""
        await self._set_entity_state(False)
