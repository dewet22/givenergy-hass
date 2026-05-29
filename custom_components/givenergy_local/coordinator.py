from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from givenergy_modbus.client.client import Client
from givenergy_modbus.exceptions import (
    PlantTopologyMismatch,
    ReadFailure,
    RefreshFailed,
    RefreshPartiallySucceeded,
)
from givenergy_modbus.model.inverter import SinglePhaseInverter
from givenergy_modbus.model.inverter_threephase import ThreePhaseInverter
from givenergy_modbus.model.plant import Plant, PlantCapabilities
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN

InverterModel = SinglePhaseInverter | ThreePhaseInverter

# Invoked after detect() raises PlantTopologyMismatch and the new topology
# has been accepted on the live client. Receives the freshly-detected
# capabilities so the caller can persist them and trigger an entry reload —
# the coordinator itself stays free of HA UI / config-entry concerns.
TopologyChangedCallback = Callable[[PlantCapabilities], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)

# Target interval between full holding-register refreshes in active mode.
# Holding registers contain largely static config (firmware, charge slots, …)
# so polling them every tick is wasteful.
_FULL_REFRESH_INTERVAL = 300  # seconds (~5 minutes)


class GivEnergyUpdateCoordinator(DataUpdateCoordinator[Plant]):
    """Wraps a long-lived Modbus Client, polling the inverter on a fixed interval.

    Concurrency invariant: detect() must not run while any other request can be
    in flight against the same client. Today this holds naturally — detect only
    runs inside _connect(), which itself runs under HA's coordinator lock and
    before any entity write path is available. Entity write calls via
    client.one_shot_command() *can* interleave with regular refresh ticks (HA's
    lock doesn't cover them), but that's safe: reads and writes have orthogonal
    shape hashes, the tx_queue serialises bytes onto the wire, and the consumer
    demuxes responses by shape hash. Moving detect onto a hot path would break
    the invariant and need a per-client lock around detect and capability
    mutation.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        scan_interval: int,
        passive: bool = False,
        timeout_tolerance: int = 3,
        retries: int = 1,
        prior_capabilities: PlantCapabilities | None = None,
        on_topology_changed: TopologyChangedCallback | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.host = host
        self.port = port
        self.passive = passive
        self.timeout_tolerance = timeout_tolerance
        self.retries = retries
        # Keep the prior across reconnects (transient TCP drops re-enter
        # _connect() and benefit from the same hint). The on-disk cache is
        # the source of truth across process restarts and is re-seeded into
        # this attribute at async_setup_entry time.
        self._prior_capabilities = prior_capabilities
        self._on_topology_changed = on_topology_changed
        self._client: Client | None = None
        self.last_successful_refresh: datetime | None = None
        self.consecutive_failures: int = 0
        self.total_failures: int = 0
        # Cumulative count of polls that returned *some* data but had one or
        # more register reads fail (RefreshPartiallySucceeded). Distinct from
        # total_failures (which counts polls that yielded no usable data) so a
        # flaky single device — e.g. dodgy RS485 wiring to one battery — shows
        # up here without eroding the hard-failure metrics.
        self.partial_failures: int = 0
        # The ReadFailure records from the most recent partial poll, surfaced as
        # a diagnostic sensor attribute so users can see *which* device dropped.
        self.last_partial_failures: list[ReadFailure] = []
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

            # A fully clean poll — clear any stale partial-failure detail so the
            # diagnostic sensor stops naming devices that have since recovered.
            # (The cumulative partial_failures counter is left untouched.)
            self.last_partial_failures = []
            self._mark_success(plant)
            return plant
        except RefreshPartiallySucceeded as exc:
            if reconnecting:
                # No reliable per-device fallback on a (re)connect seed: a
                # half-populated initial plant (the dropped device reads
                # "unknown") is worse than a clean full retry. detect() already
                # gated device presence, so a partial seed is most likely a
                # transient hiccup on a present device. Route through the same
                # tolerance gate as a timeout — serves last-known data on an
                # in-process reconnect, or raises UpdateFailed (→
                # ConfigEntryNotReady) on a cold start so HA retries setup.
                self._record_failure()
                return await self._serve_last_known_or_fail("Partial data on (re)connect seed", exc)
            # Steady state: serve the partial. The dropped device's last-known
            # register values ride along (frozen) while the rest stay fresh —
            # this is the behaviour change #125 buys us (one offline battery no
            # longer discards every other reading for the tick).
            self._record_partial(exc)
            self._mark_success(exc.plant)
            return exc.plant
        except UpdateFailed:
            self._record_failure()
            raise
        except RefreshFailed as err:
            self._record_failure()
            if self._is_timeout_failure(err):
                # Every read timed out — treat like the bare-TimeoutError path
                # below: transient, keep the client and serve last-known data
                # until the tolerance window is exhausted.
                return await self._serve_last_known_or_fail(
                    "Timed out communicating with inverter", err
                )
            await self._reset_client()
            raise UpdateFailed(f"Error communicating with inverter: {err}") from err
        except TimeoutError as err:
            # Defensive: a timeout not wrapped in RefreshFailed. Keep the client
            # alive — timeouts are transient and the TCP connection is likely
            # still valid.
            self._record_failure()
            return await self._serve_last_known_or_fail(
                "Timed out communicating with inverter", err
            )
        except Exception as err:
            self._record_failure()
            await self._reset_client()
            raise UpdateFailed(
                f"Error communicating with inverter: {str(err) or type(err).__name__}"
            ) from err

    # ------------------------------------------------------------------
    # Failure / success bookkeeping
    # ------------------------------------------------------------------

    def _mark_success(self, plant: Plant) -> None:
        """Record a tick that yielded usable data (full or partial success)."""
        self._last_inverter_time = plant.inverter.system_time
        self.last_successful_refresh = dt_util.utcnow()
        self.consecutive_failures = 0

    def _record_failure(self) -> None:
        """Count a poll that yielded no usable data."""
        self.consecutive_failures += 1
        self.total_failures += 1

    def _record_partial(self, exc: RefreshPartiallySucceeded) -> None:
        """Count a degraded-but-usable poll and surface which reads dropped."""
        self.partial_failures += 1
        self.last_partial_failures = exc.failures
        _LOGGER.warning(
            "Partial refresh: %d register read(s) failed; serving last-known "
            "values for those banks. Failures: %s",
            len(exc.failures),
            exc.failures,
        )

    def _is_timeout_failure(self, err: RefreshFailed) -> bool:
        """True if every underlying cause of a RefreshFailed is a timeout.

        Timeout-only failures are treated as transient (tolerance window);
        anything else resets the client and fails immediately.
        """
        cause = err.cause
        if isinstance(cause, BaseExceptionGroup):
            _, rest = cause.split(TimeoutError)
            return rest is None
        return isinstance(cause, TimeoutError)

    async def _serve_last_known_or_fail(self, message: str, err: BaseException) -> Plant:
        """Serve last-known data within the tolerance window, else reset and fail.

        Mirrors the long-standing timeout-tolerance behaviour: a transient blip
        keeps the client and replays `self.data` up to `timeout_tolerance`
        consecutive failures; past that (or with no data yet) the client is
        reset and UpdateFailed raised.
        """
        if self.data is not None and self.consecutive_failures < self.timeout_tolerance:
            _LOGGER.warning(
                "%s (failure %d/%d); serving last known data",
                message,
                self.consecutive_failures,
                self.timeout_tolerance,
            )
            return self.data
        await self._reset_client()
        raise UpdateFailed(f"{message} ({self.consecutive_failures} consecutive failures)") from err

    # ------------------------------------------------------------------
    # Update strategies
    # ------------------------------------------------------------------

    async def _active_update(self) -> Plant:
        """Alternate between full and partial Modbus refreshes.

        Holding registers (config, charge slots, …) are re-read only every
        _full_refresh_every ticks; input registers (real-time data) are read
        every tick.

        Raises RefreshPartiallySucceeded / RefreshFailed straight up to
        _async_update_data, which owns the seed-vs-steady-state policy (it's the
        only caller that knows whether this tick is a reconnect seed).
        """
        assert self._client is not None  # _async_update_data ensures this
        full_refresh = self._active_tick % self._full_refresh_every == 0
        self._active_tick += 1
        if full_refresh:
            await self._client.load_config(retries=self.retries)
        return await self._client.refresh(retries=self.retries)

    async def _passive_update(self, reconnecting: bool) -> Plant:
        """Seed the cache on (re)connect; on subsequent ticks read the cached plant.

        The library's register cache is kept fresh by a peer client on the
        shared Modbus bus.  Raises UpdateFailed if the cache appears frozen.
        """
        assert self._client is not None  # _async_update_data ensures this
        if reconnecting:
            await self._client.load_config(retries=self.retries)
            return await self._client.refresh(retries=self.retries)

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
        """Open a fresh TCP connection, discover topology, and reset staleness tracking.

        Passes `prior=self._prior_capabilities` so the library can skip the
        full peripheral-probe sweep when the topology hasn't changed since the
        last process startup. On `PlantTopologyMismatch` the new topology is
        accepted on the live client and the caller-provided callback is
        invoked to persist it and schedule a reload — the reload is needed
        because entity counts (notably batteries) are frozen at platform
        setup time.

        detect() populates plant.capabilities, which makes subsequent
        refresh_plant() calls dispatch via model-aware load_config()/refresh()
        — required for three-phase, AIO-HV, EMS and other non-default topologies.
        """
        self._client = Client(host=self.host, port=self.port)
        await self._client.connect()
        try:
            await self._client.detect(prior=self._prior_capabilities)
        except PlantTopologyMismatch as exc:
            _LOGGER.warning(
                "Plant topology has changed since last seen — accepting new layout "
                "(prior=%r, actual=%r); a reload will refresh entity counts",
                exc.prior,
                exc.actual,
            )
            # Library leaves plant.capabilities=None on mismatch; assign so this
            # tick's refresh_plant() still dispatches correctly using the new
            # topology before the reload tears things down.
            assert self._client is not None  # set two lines above
            self._client.plant.capabilities = exc.actual
            self._prior_capabilities = exc.actual
            if self._on_topology_changed is not None:
                await self._on_topology_changed(exc.actual)
        self._last_inverter_time = None
        self._unchanged_ticks = 0
        self._active_tick = 0
        _LOGGER.info("Connected to inverter at %s:%s", self.host, self.port)

    async def _reset_client(self) -> None:
        """Close and discard the client so the next tick triggers a reconnect."""
        if self._client is not None:
            _LOGGER.info("Closing connection to %s:%s", self.host, self.port)
            await self._client.close()
            self._client = None

    async def async_close(self) -> None:
        await self._reset_client()
