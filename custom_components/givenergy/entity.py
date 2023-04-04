"""GivEnergy entity classes."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION, MANUFACTURER
from .coordinator import GivEnergyCoordinator


class InverterEntity(CoordinatorEntity[GivEnergyCoordinator]):
    """Entity representing an inverter."""

    def __init__(self, coordinator: GivEnergyCoordinator) -> None:
        super().__init__(coordinator)
        plant = self.coordinator.data
        inverter = plant.inverter

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant.inverter_serial_number)},
            name=f"Inverter {plant.inverter_serial_number}",
            model=inverter.inverter_model.name,
            manufacturer=MANUFACTURER,
            sw_version=inverter.inverter_firmware_version,
            configuration_url=f"http://{self.coordinator.client.host}:{self.coordinator.client.port}",
        )


class BatteryEntity(CoordinatorEntity[GivEnergyCoordinator]):
    """Entity representing a battery."""

    def __init__(self, coordinator: GivEnergyCoordinator, battery_id: int) -> None:
        super().__init__(coordinator)
        plant = self.coordinator.data
        battery = plant.batteries[battery_id]

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, battery.battery_serial_number)},
            name=f"Battery {battery.battery_serial_number}",
            model=f'{battery.design_capacity}Ah',
            manufacturer=MANUFACTURER,
            sw_version=str(battery.bms_firmware_version),
            via_device=(DOMAIN, plant.inverter_serial_number),
        )
