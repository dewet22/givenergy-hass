"""DataUpdateCoordinator for givenergy."""
from __future__ import annotations

import asyncio
from datetime import timedelta, datetime

from givenergy_modbus.client import commands
from givenergy_modbus.client.client import Client
from givenergy_modbus.exceptions import CommunicationError
from givenergy_modbus.model.battery import Battery
from givenergy_modbus.model.inverter import Inverter
from givenergy_modbus.model.plant import Plant

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, LOGGER


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class GivEnergyCoordinator(DataUpdateCoordinator[Plant]):
    """Class to coordinate data refreshes using the givenergy_modbus library."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, client: Client, update_interval: timedelta,
                 full_refresh_interval: timedelta) -> None:
        """Initialize."""
        self.client = client
        self.full_refresh_interval = full_refresh_interval
        self.last_full_refresh = datetime.min
        self.last_refresh = datetime.min
        super().__init__(hass=hass, logger=LOGGER, name="Plant", update_interval=update_interval)

    @property
    def plant(self) -> Plant:
        return self.client.plant

    @property
    def inverter(self) -> Inverter:
        return self.plant.inverter

    @property
    def batteries(self) -> list[Battery]:
        return self.plant.batteries

    async def _async_update_data(self):
        """Update data via library."""
        utcnow = datetime.utcnow()
        # await self.connect()

        try:
            if self.last_full_refresh + self.full_refresh_interval < utcnow:
                LOGGER.info('Doing full refresh')
                await self.client.execute(
                    commands.refresh_plant_data(True),
                    timeout=1.0, retries=3)
                self.last_full_refresh = utcnow
            else:
                LOGGER.debug('Doing quick refresh')
                await self.client.execute(
                    commands.refresh_plant_data(False, number_batteries=self.plant.number_batteries),
                    timeout=1.0, retries=2)
        except CommunicationError as e:
            raise UpdateFailed(e) from e
        except asyncio.TimeoutError as e:
            data_age = utcnow - self.last_refresh
            if data_age > self.update_interval * 5:
                # LOGGER.error('Data fetching seems broken.')
                raise UpdateFailed(e) from e
            LOGGER.warning(
                f'Timeout refreshing data, will retry. Current data is {data_age.seconds} seconds stale.')
        else:
            self.last_refresh = utcnow
        # finally:
        #     LOGGER.info(
        #         f'qsize={self.client.tx_queue.qsize()} '
        #         f'network_producer={self.client.network_producer_task._state}:{self.client.network_producer_task.get_stack()[0].f_lineno} '
        #         f'network_consumer={self.client.network_consumer_task._state}:{self.client.network_consumer_task.get_stack()[0].f_lineno} '
        #         f'last_message_consumed@{self.client.last_message_consumed.strftime("%H:%M:%S")}={self.client.network_consumer_task.get_stack()[0].f_locals["message"]} '
        #         f'last_message_produced@{self.client.last_message_produced.strftime("%H:%M:%S")} '
        #     )
        #     # await self.close()

        return self.plant

    async def connect(self):
        await self.client.connect()

    async def close(self):
        await self.client.close()

    async def set_entity_state(self, key: str, value: bool | int | float):
        if func := getattr(commands, f'set_{key}', None):
            return await self.client.execute(func(value), timeout=1, retries=3)
        LOGGER.error(f'Unknown entity to set state: {key}')
