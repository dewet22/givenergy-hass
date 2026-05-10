from __future__ import annotations

import logging
from datetime import timedelta

from givenergy_modbus.client.client import Client
from givenergy_modbus.model.plant import Plant
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GivEnergyUpdateCoordinator(DataUpdateCoordinator[Plant]):
    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        scan_interval: int,
        max_batteries: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.host = host
        self.port = port
        self.max_batteries = max_batteries
        self._client: Client | None = None

    async def _async_update_data(self) -> Plant:
        try:
            if self._client is None or not self._client.connected:
                self._client = Client(host=self.host, port=self.port)
                await self._client.connect()

            return await self._client.refresh_plant(
                full_refresh=True,
                max_batteries=self.max_batteries,
            )
        except Exception as err:
            if self._client is not None:
                await self._client.close()
                self._client = None
            raise UpdateFailed(f"Error communicating with inverter: {err}") from err

    async def async_close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
