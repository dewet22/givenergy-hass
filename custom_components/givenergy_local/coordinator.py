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

# Target interval between full holding-register refreshes in active mode.
# Holding registers contain largely static config (firmware, charge slots, …)
# so polling them every tick is wasteful.
_FULL_REFRESH_INTERVAL = 300  # seconds (~5 minutes)


class GivEnergyUpdateCoordinator(DataUpdateCoordinator[Plant]):
    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        scan_interval: int,
        max_batteries: int,
        passive: bool = False,
        timeout_tolerance: int = 5,
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
        self.timeout_tolerance = timeout_tolerance
        self._client: Client | None = None
        self.last_successful_refresh: datetime | None = None
        self.consecutive_failures: int = 0
        self.total_failures: int = 0
        self._last_inverter_time: datetime | None = None
        self._unchanged_ticks: int = 0
        self._active_tick: int = 0
        self._full_refresh_every: int = max(1, round(_FULL_REFRESH_INTERVAL / scan_interval))

    # ------------------------------------------------------------------
    # DataUpdateCoordinator entry point
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> Plant:
        try:
            reconnecting = self._client is None or not self._client.connected
            if reconnecting:
                await self._connect()

            plant = (
                await self._passive_update(reconnecting)
                if self.passive
                else await self._active_update()
            )

            self._last_inverter_time = plant.inverter.system_time
            self.last_successful_refresh = dt_util.utcnow()
            self.consecutive_failures = 0
            return plant
        except UpdateFailed:
            self.consecutive_failures += 1
            self.total_failures += 1
            raise
        except TimeoutError:
            # Keep the client alive — timeouts are transient and the TCP
            # connection is likely still valid.
            self.consecutive_failures += 1
            self.total_failures += 1
            if self.data is None or self.consecutive_failures > self.timeout_tolerance:
                raise UpdateFailed(
                    f"Timed out communicating with inverter "
                    f"({self.consecutive_failures} consecutive failures)"
                )
            _LOGGER.warning(
                "Timed out communicating with inverter (failure %d/%d); serving last known data",
                self.consecutive_failures,
                self.timeout_tolerance,
            )
            return self.data
        except Exception as err:
            self.consecutive_failures += 1
            self.total_failures += 1
            await self._reset_client()
            raise UpdateFailed(
                f"Error communicating with inverter: {str(err) or type(err).__name__}"
            ) from err

    # ------------------------------------------------------------------
    # Update strategies
    # ------------------------------------------------------------------

    async def _active_update(self) -> Plant:
        """Alternate between full and partial Modbus refreshes.

        Holding registers (config, charge slots, …) are re-read only every
        _full_refresh_every ticks; input registers (real-time data) are read
        every tick.
        """
        assert self._client is not None  # _async_update_data ensures this
        full_refresh = self._active_tick % self._full_refresh_every == 0
        self._active_tick += 1
        await self._client.refresh_plant(
            full_refresh=full_refresh,
            max_batteries=self.max_batteries,
        )
        return self._client.plant

    async def _passive_update(self, reconnecting: bool) -> Plant:
        """Seed the cache on (re)connect; on subsequent ticks read the cached plant.

        The library's register cache is kept fresh by a peer client on the
        shared Modbus bus.  Raises UpdateFailed if the cache appears frozen.
        """
        assert self._client is not None  # _async_update_data ensures this
        if reconnecting:
            await self._client.refresh_plant(
                full_refresh=True,
                max_batteries=self.max_batteries,
            )
            return self._client.plant

        plant = self._client.plant
        self._check_cache_freshness(plant)
        return plant

    def _check_cache_freshness(self, plant: Plant) -> None:
        """Raise UpdateFailed if system_time hasn't advanced for two consecutive ticks.

        One unchanged tick is tolerated to absorb timing skew between our poll
        interval and the peer's refresh cadence.
        """
        inverter_time = plant.inverter.system_time
        if (
            inverter_time is not None
            and self._last_inverter_time is not None
            and inverter_time == self._last_inverter_time
        ):
            self._unchanged_ticks += 1
            if self._unchanged_ticks >= 2:
                raise UpdateFailed(
                    "Register cache unchanged for 2 consecutive ticks — "
                    "no peer client appears to be refreshing the inverter"
                )
        else:
            self._unchanged_ticks = 0

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Open a fresh TCP connection and reset all staleness tracking."""
        self._client = Client(host=self.host, port=self.port)
        await self._client.connect()
        self._last_inverter_time = None
        self._unchanged_ticks = 0
        self._active_tick = 0

    async def _reset_client(self) -> None:
        """Close and discard the client so the next tick triggers a reconnect."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def async_close(self) -> None:
        await self._reset_client()
