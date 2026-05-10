from __future__ import annotations

import logging
from datetime import datetime, timedelta

from givenergy_modbus.client.client import Client
from givenergy_modbus.model.plant import Plant
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
        passive: bool = False,
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
        self.passive = passive
        self._client: Client | None = None
        self.last_successful_refresh: datetime | None = None
        self.consecutive_failures: int = 0
        self._last_inverter_time: datetime | None = None
        self._unchanged_ticks: int = 0

    async def _async_update_data(self) -> Plant:
        try:
            reconnecting = self._client is None or not self._client.connected
            if reconnecting:
                self._client = Client(host=self.host, port=self.port)
                await self._client.connect()
                self._last_inverter_time = None
                self._unchanged_ticks = 0

            # Always issue a full refresh on (re)connect to seed the cache.
            # In passive mode subsequent ticks just read the library's cache,
            # which the shared-bus peer keeps fresh via its own requests.
            if reconnecting or not self.passive:
                await self._client.refresh_plant(
                    full_refresh=True,
                    max_batteries=self.max_batteries,
                )

            plant = self._client.plant

            # In passive mode, use the inverter's RTC as a cache-freshness
            # signal. Two consecutive ticks with an identical system_time means
            # the register cache hasn't been updated — the peer client has
            # stopped refreshing or is absent. One unchanged tick is tolerated
            # to absorb timing skew between our poll interval and the peer's.
            if self.passive and not reconnecting:
                inverter_time = plant.inverter.system_time
                if (
                    inverter_time is not None
                    and self._last_inverter_time is not None
                    and inverter_time == self._last_inverter_time
                ):
                    self._unchanged_ticks += 1
                    if self._unchanged_ticks >= 2:
                        self.consecutive_failures += 1
                        raise UpdateFailed(
                            "Register cache unchanged for 2 consecutive ticks — "
                            "no peer client appears to be refreshing the inverter"
                        )
                else:
                    self._unchanged_ticks = 0

            self._last_inverter_time = plant.inverter.system_time
            self.last_successful_refresh = dt_util.utcnow()
            self.consecutive_failures = 0
            return plant
        except UpdateFailed:
            raise
        except TimeoutError as err:
            # Keep the client alive — timeouts are transient and the TCP
            # connection is likely still valid.
            self.consecutive_failures += 1
            raise UpdateFailed(f"Timed out communicating with inverter: {err}") from err
        except Exception as err:
            self.consecutive_failures += 1
            if self._client is not None:
                await self._client.close()
                self._client = None
            raise UpdateFailed(f"Error communicating with inverter: {err}") from err

    async def async_close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
