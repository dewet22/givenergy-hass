from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

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

# Invoked when detect() reports that a previously-known device did NOT respond
# (a loss) and the loss persisted across retries. Receives (prior, actual) so
# the caller can raise a loud, fixable repair WITHOUT persisting the reduced
# topology — the full prior stays cached so the next reconnect re-probes it.
DevicesMissingCallback = Callable[[PlantCapabilities, PlantCapabilities], Awaitable[None]]

# Invoked whenever a (re)connect confirms the full expected topology (detect
# succeeded with no mismatch, or a loss healed on retry), with the confirmed
# capabilities. Lets the caller clear a stale "device missing" repair the
# moment the device answers again, and reconcile entities against what is now
# confirmed — a device that answered late never got entities at platform
# setup, so the caller reloads the entry when the heal reveals one (#148).
TopologyHealedCallback = Callable[[PlantCapabilities], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)

# Target interval between full holding-register refreshes in active mode.
# Holding registers contain largely static config (firmware, charge slots, …)
# so polling them every tick is wasteful.
_FULL_REFRESH_INTERVAL = 300  # seconds (~5 minutes)

# During a sustained run of partial polls, re-emit the partial warning at this
# cadence (in polls) instead of every cycle. The first partial of a run always
# warns; the in-between ones drop to DEBUG so a flaky/contended plant doesn't
# flood the log. The cumulative partial_failures counter/sensor is unaffected.
_PARTIAL_LOG_EVERY = 20

# Per-slot probe budget for detect()'s peripheral sweep (batteries, meters).
# More generous than the library defaults (0.5s / 1 retry): the battery sweep
# breaks at the first non-responding slot, so a transiently slow BMS can
# truncate the whole chain. The break means at most one empty slot is probed,
# so the extra latency is bounded to ~one slow probe per detect.
PROBE_TIMEOUT_SECONDS = 1.0
PROBE_RETRIES = 3

# When detect() reports a DEVICE LOSS (a previously-known battery/meter/stack
# stopped answering), re-probe a few times before believing it — a slow BMS
# often answers on the next sweep. Kept small: this blocks _connect(), which
# runs under HA's coordinator lock, so total added latency on a persistent loss
# is bounded to DETECT_LOSS_RETRIES * (one detect sweep + DETECT_LOSS_RETRY_DELAY).
DETECT_LOSS_RETRIES = 2
DETECT_LOSS_RETRY_DELAY = 5.0  # seconds
LOSS_REDETECT_INTERVAL = 300.0  # seconds between loss-driven reconnect-and-detect attempts


def missing_devices(prior: PlantCapabilities | None, actual: PlantCapabilities) -> list[str]:
    """Describe devices present in ``prior`` but absent from ``actual``.

    An empty list means this is *not* a device loss — a pure add or a
    ``device_type`` change returns ``[]`` so the routine accept-persist-reload
    path handles it. A non-empty list means a previously-known device is gone
    (the descriptors double as the repair message), which the caller should
    retry and, if persistent, surface loudly rather than silently bake in.

    Membership comparison (not positional) — the library may reorder addresses;
    only an *absence* (or a shrunk HV module count) counts as a loss.
    """
    if prior is None or prior.device_type != actual.device_type:
        return []
    missing: list[str] = []
    for addr in prior.lv_battery_addresses:
        if addr not in actual.lv_battery_addresses:
            missing.append(f"battery at 0x{addr:02x}")
    for addr in prior.meter_addresses:
        if addr not in actual.meter_addresses:
            missing.append(f"meter at 0x{addr:02x}")
    for addr in prior.aio_battery_module_addresses:
        if addr not in actual.aio_battery_module_addresses:
            missing.append(f"AIO battery module at 0x{addr:02x}")
    # bcu_stacks entries are (bcu_offset, num_modules); device addr = 0x70+offset.
    actual_modules_by_offset = {off: mods for off, mods in actual.bcu_stacks}
    for off, mods in prior.bcu_stacks:
        if off not in actual_modules_by_offset:
            missing.append(f"HV stack at 0x{0x70 + off:02x}")
        elif actual_modules_by_offset[off] < mods:
            missing.append(
                f"HV stack at 0x{0x70 + off:02x} "
                f"({actual_modules_by_offset[off]} of {mods} modules)"
            )
    return missing


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
        experimental_client_kwargs: dict[str, Any] | None = None,
        timeout_tolerance: int = 3,
        retries: int = 1,
        prior_capabilities: PlantCapabilities | None = None,
        on_topology_changed: TopologyChangedCallback | None = None,
        on_devices_missing: DevicesMissingCallback | None = None,
        on_topology_healed: TopologyHealedCallback | None = None,
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
        # Resolved opt-in givenergy-modbus client kwargs (empty by default, so
        # Client(host, port) is unchanged). Splatted at every (re)connect.
        self._experimental_client_kwargs = experimental_client_kwargs or {}
        self.timeout_tolerance = timeout_tolerance
        self.retries = retries
        # Keep the prior across reconnects (transient TCP drops re-enter
        # _connect() and benefit from the same hint). The on-disk cache is
        # the source of truth across process restarts and is re-seeded into
        # this attribute at async_setup_entry time.
        self._prior_capabilities = prior_capabilities
        self._on_topology_changed = on_topology_changed
        self._on_devices_missing = on_devices_missing
        self._on_topology_healed = on_topology_healed
        self._client: Client | None = None
        self._schedule_reconnect: bool = False
        self._loss_redetect_after: float = 0.0
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
        # Deliberately NOT cleared by a subsequent clean poll (#176): an
        # intermittent failure would otherwise be un-diagnosable after the fact —
        # the detail must outlive the recovery so the sensor can still name the
        # bank that dropped, paired with last_partial_at below.
        self.last_partial_failures: list[ReadFailure] = []
        # When the most recent partial poll occurred (UTC), retained alongside
        # last_partial_failures so the diagnostic sensor can show how long ago.
        self.last_partial_at: datetime | None = None
        # Comms-quality noise floor: per-device cumulative counts of CRC-failed
        # responses, splice-guard trips, and read retries consumed — mirrored
        # from the library's own Plant counters (which reset on each Client/Plant
        # re-instantiation). We accumulate reset-aware deltas here so the
        # diagnostic sensors stay monotonic across reconnects, like
        # total_failures. Keyed by device address; the headline sensor value is
        # the sum across devices.
        self.crc_failures_by_device: dict[int, int] = {}
        self.splice_rejections_by_device: dict[int, int] = {}
        self.splice_holds_by_device: dict[int, int] = {}
        self.read_retries_by_device: dict[int, int] = {}
        # Cold-start battery-baseline holds (modbus #289, 2.5.5): a benign
        # "establishing baseline" signal, distinct from splice_holds (corruption).
        self.cold_start_held_by_device: dict[int, int] = {}
        # Last cumulative value seen per library attr per device, for delta-ing.
        self._comms_last_seen: dict[str, dict[int, int]] = {}
        # Length of the current unbroken run of partial polls, used only to
        # throttle the partial log (see _record_partial). Reset by a clean poll.
        self._consecutive_partials: int = 0
        self._last_inverter_time: datetime | None = None
        self._unchanged_ticks: int = 0
        self._active_tick: int = 0
        self._full_refresh_every: int = max(1, round(_FULL_REFRESH_INTERVAL / scan_interval))

    # ------------------------------------------------------------------
    # DataUpdateCoordinator entry point
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> Plant:
        try:
            loop = asyncio.get_running_loop()
            if self._schedule_reconnect and loop.time() >= self._loss_redetect_after:
                self._schedule_reconnect = False
                await self._reset_client()
                loop = asyncio.get_running_loop()
                self._loss_redetect_after = loop.time() + LOSS_REDETECT_INTERVAL
            reconnecting = self._client is None or not self._client.connected
            if reconnecting:
                await self._connect()
            if self._client is None:
                # The entry was unloaded while _connect() was resolving topology
                # (async_close() discarded the client mid-flight). Serve the
                # last-known data for this final tick rather than proceeding to
                # a poll that would fail loudly — the coordinator is being torn
                # down, and a routine unload should not log an ERROR.
                return self.data

            plant = (
                await self._passive_update(reconnecting)
                if self.passive
                else await self._active_update()
            )

            # A fully clean poll ends the partial run so the next one warns afresh.
            # The last_partial_failures detail and last_partial_at are deliberately
            # retained (#176) so the diagnostic sensor can still name the bank that
            # dropped after an intermittent failure has recovered.
            self._consecutive_partials = 0
            self._mark_success(plant)
            return plant
        except RefreshPartiallySucceeded as exc:
            if reconnecting:
                if self.data is None and exc.plant.inverter_serial_number:
                    # Cold start with an identified inverter: serve the partial so
                    # the integration *loads* (the failed reads' entities go
                    # unavailable — a structurally-absent block like AC-config on a
                    # hybrid, or a flaky device) instead of looping forever in
                    # ConfigEntryNotReady. Those entities recover automatically if a
                    # later poll reads them. _record_partial sets
                    # last_partial_failures, which also blocks the cold-start
                    # capabilities persist in __init__ — never commit a degraded
                    # topology to disk.
                    self._record_partial(exc)
                    self._mark_success(exc.plant)
                    return exc.plant
                # Either an in-process reconnect (serve last-known within the
                # tolerance window — unchanged) or a cold start too sparse to set up
                # at all (no inverter identity → raise → ConfigEntryNotReady, retry).
                self._record_failure()
                failure_summary = ", ".join(
                    f"{'HR' if 'Holding' in f.request_type else 'IR'}({f.base_register})"
                    f" on device 0x{f.device_address:02x}"
                    for f in exc.failures
                )
                return await self._serve_last_known_or_fail(
                    f"Partial data on (re)connect seed: {failure_summary}",
                    exc,
                )
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
        self._accumulate_comms_counters(plant)

    # (library Plant attr, our per-device cumulative dict attr)
    _COMMS_COUNTER_SOURCES = (
        ("crc_failure_count", "crc_failures_by_device"),
        ("splice_reject_count", "splice_rejections_by_device"),
        ("splice_held_count", "splice_holds_by_device"),
        ("retry_count", "read_retries_by_device"),
        ("cold_start_held_count", "cold_start_held_by_device"),
    )

    def _accumulate_comms_counters(self, plant: Plant) -> None:
        """Fold the library's per-device comms-quality counters into our own.

        givenergy-modbus exposes cumulative-since-construction counters for
        CRC-failed responses and splice-guard trips (keyed by device address);
        they reset whenever the Client/Plant is re-instantiated (e.g. on a
        reconnect). We mirror them into coordinator-level dicts that survive
        reconnects, accumulating reset-aware deltas so the diagnostic sensors
        stay monotonic (like total_failures). Reads are defensive: on a modbus
        build that predates these counters the attribute is absent (or, under a
        MagicMock test plant, not a dict), so the counters simply stay at zero.
        """
        for lib_attr, dest_attr in self._COMMS_COUNTER_SOURCES:
            lib_counts = getattr(plant, lib_attr, None)
            if not isinstance(lib_counts, dict):
                continue
            dest: dict[int, int] = getattr(self, dest_attr)
            last_seen = self._comms_last_seen.setdefault(lib_attr, {})
            for device, current in lib_counts.items():
                last = last_seen.get(device, 0)
                # current < last ⇒ the library counter reset (fresh Plant);
                # treat the new value as the delta rather than going negative.
                delta = current - last if current >= last else current
                if delta:
                    dest[device] = dest.get(device, 0) + delta
                last_seen[device] = current

    def _record_failure(self) -> None:
        """Count a poll that yielded no usable data."""
        self.consecutive_failures += 1
        self.total_failures += 1

    def _record_partial(self, exc: RefreshPartiallySucceeded) -> None:
        """Count a degraded-but-usable poll and surface which reads dropped.

        Partials are expected on flaky/contended plants (device-level dongle
        garbage, multi-client contention), so the per-poll log is throttled:
        the first partial of a run warns, a sustained run re-warns every
        ``_PARTIAL_LOG_EVERY`` polls, and the rest drop to DEBUG. The cumulative
        ``partial_failures`` counter and the diagnostic sensor are unaffected.
        """
        self.partial_failures += 1
        self.last_partial_failures = exc.failures
        self.last_partial_at = dt_util.utcnow()
        self._consecutive_partials += 1
        if self._consecutive_partials == 1:
            _LOGGER.warning(
                "Partial refresh: %d register read(s) failed; serving last-known "
                "values for those banks (further partials this run at debug). "
                "Failures: %s",
                len(exc.failures),
                exc.failures,
            )
        elif self._consecutive_partials % _PARTIAL_LOG_EVERY == 0:
            _LOGGER.warning(
                "Partial refresh still occurring (%d consecutive polls); serving "
                "last-known values. Failures: %s",
                self._consecutive_partials,
                exc.failures,
            )
        else:
            _LOGGER.debug(
                "Partial refresh: %d register read(s) failed; serving last-known. Failures: %s",
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
        self._client = Client(host=self.host, port=self.port, **self._experimental_client_kwargs)
        # A fresh Client means a fresh Plant with its comms counters zeroed, so
        # drop the per-device last-seen baselines: the next poll then captures
        # the full post-reconnect value as the delta. Without this, a counter
        # that climbs back past its pre-reconnect value before the first poll
        # would be mistaken for monotonic growth and undercounted.
        self._comms_last_seen.clear()
        try:
            await self._client.connect()
            topology_confirmed = True
            try:
                # The library's battery sweep probes additional packs (0x33+) with a
                # stingy default budget (probe_timeout=0.5s, probe_retries=1) and
                # BREAKS at the first non-responding slot — so a single transiently
                # slow BMS during a reconnect truncates the whole battery chain, and
                # that reduced topology then gets persisted (a 2nd battery silently
                # vanished after a reconnect this way). Because of the break, a detect
                # probes at most one empty slot, so a generous probe budget costs only
                # ~one extra slow probe on a cold sweep — cheap insurance against
                # dropping a real pack.
                await self._client.detect(
                    prior=self._prior_capabilities,
                    probe_timeout=PROBE_TIMEOUT_SECONDS,
                    probe_retries=PROBE_RETRIES,
                )
            except PlantTopologyMismatch as exc:
                topology_confirmed = await self._handle_topology_mismatch(exc)
            if self._client is None:
                # The entry was unloaded/reloaded while _handle_topology_mismatch
                # yielded (retry sleeps) and async_close() discarded the client —
                # nothing to confirm against, we're shutting down.
                return
            confirmed = self._client.plant.capabilities
            if (
                topology_confirmed
                and self._on_topology_healed is not None
                and confirmed is not None
            ):
                # Full expected topology is present — let the caller clear any
                # stale "device missing" repair and reconcile entities against the
                # confirmed topology (covers both restart and mid-session recovery).
                try:
                    await self._on_topology_healed(confirmed)
                except Exception:  # noqa: BLE001
                    # The connection is up and detect() succeeded; a failure in
                    # the caller's reconcile/repair callback must not discard the
                    # freshly-connected client (which the outer BaseException
                    # handler would do) or fail the poll. Log and carry on — the
                    # stale repair issue, if any, simply clears on a later heal.
                    _LOGGER.warning(
                        "topology-healed callback failed; connection is up, continuing",
                        exc_info=True,
                    )
            self._last_inverter_time = None
            self._unchanged_ticks = 0
            self._active_tick = 0
            _LOGGER.info("Connected to inverter at %s:%s", self.host, self.port)
        except BaseException:
            # connect()/detect() failed before capabilities were established (a
            # PlantTopologyMismatch is handled above and doesn't reach here). Discard
            # the half-initialised client so the next tick reconnects and re-detects
            # cleanly — a connected-but-capability-less client would otherwise be kept
            # by the timeout-tolerance path, and its next refresh() raises
            # PlantNotDetected ("requires plant capabilities") (#176). BaseException,
            # not Exception, so a CancelledError (HA cancelling the connect mid-flight)
            # also cleans up rather than leaking the client; re-raised either way.
            await self._reset_client()
            raise

    async def _handle_topology_mismatch(self, exc: PlantTopologyMismatch) -> bool:
        """Resolve a detect() topology mismatch.

        Returns True only when the full expected topology is confirmed (a loss
        that healed on retry) so the caller can clear a stale repair. Returns
        False for a persistent loss (surfaced via on_devices_missing, prior kept)
        and for a routine add / device_type change (accepted, persisted, reloaded
        via on_topology_changed).
        """
        assert self._client is not None  # _connect set it before calling

        missing = missing_devices(exc.prior, exc.actual)
        if missing:
            # A previously-known device didn't answer. Re-probe a few times — a
            # slow BMS often responds on the next sweep — before believing it.
            for attempt in range(1, DETECT_LOSS_RETRIES + 1):
                _LOGGER.warning(
                    "detect() reports missing device(s) %s (attempt %d/%d); "
                    "re-probing in %.0fs before accepting the loss",
                    missing,
                    attempt,
                    DETECT_LOSS_RETRIES,
                    DETECT_LOSS_RETRY_DELAY,
                )
                await asyncio.sleep(DETECT_LOSS_RETRY_DELAY)
                if self._client is None:
                    # The entry was unloaded during the retry sleep and
                    # async_close() discarded the client — abandon the
                    # resolution quietly; no callbacks for a dead entry.
                    _LOGGER.debug(
                        "Client discarded during loss-retry sleep (entry unloading); "
                        "abandoning topology resolution"
                    )
                    return False
                try:
                    await self._client.detect(
                        prior=self._prior_capabilities,
                        probe_timeout=PROBE_TIMEOUT_SECONDS,
                        probe_retries=PROBE_RETRIES,
                    )
                except PlantTopologyMismatch as retry_exc:
                    # A retry can surface a *different* mismatch (e.g. an add now);
                    # re-classify against this retry's prior/actual.
                    exc = retry_exc
                    missing = missing_devices(retry_exc.prior, retry_exc.actual)
                    if not missing:
                        break  # no longer a loss → routine add/device_type path
                    continue
                else:
                    # Retry succeeded: detect() repopulated plant.capabilities with
                    # the full prior. Nothing to bake in, no callback, prior kept.
                    _LOGGER.info("Missing device(s) reappeared on retry; full topology restored")
                    return True

        if self._client is None:
            # Unloaded while the final detect() retry was in flight — same
            # bail-out as above, just past the loop rather than inside it.
            _LOGGER.debug(
                "Client discarded during topology resolution (entry unloading); abandoning"
            )
            return False

        if missing:
            # Persistent loss. Do NOT touch self._prior_capabilities — keep the
            # full prior so the next reconnect re-probes it and self-heals. Serve
            # the reduced topology for this tick only so the live poll dispatches
            # for what responded; the missing device's entities read unavailable.
            _LOGGER.error(
                "Expected device(s) %s did not respond after %d retries; serving the "
                "reduced topology for this poll but NOT persisting it — affected "
                "entities will read unavailable until they reappear or you re-detect",
                missing,
                DETECT_LOSS_RETRIES,
            )
            self._client.plant.capabilities = exc.actual
            self._schedule_reconnect = True
            if self._on_devices_missing is not None:
                await self._on_devices_missing(exc.prior, exc.actual)
            return False

        # Not (or no longer) a loss: routine add / device_type change. Preserve
        # the existing behaviour — accept, persist, and reload via the callback.
        _LOGGER.warning(
            "Plant topology has changed since last seen — accepting new layout "
            "(prior=%r, actual=%r); a reload will refresh entity counts",
            exc.prior,
            exc.actual,
        )
        # Library leaves plant.capabilities=None on mismatch; assign so this
        # tick's refresh_plant() still dispatches correctly using the new
        # topology before the reload tears things down.
        self._client.plant.capabilities = exc.actual
        self._prior_capabilities = exc.actual
        if self._on_topology_changed is not None:
            await self._on_topology_changed(exc.actual)
        return False

    async def _reset_client(self) -> None:
        """Close and discard the client so the next tick triggers a reconnect."""
        if self._client is not None:
            _LOGGER.info("Closing connection to %s:%s", self.host, self.port)
            try:
                await self._client.close()
            except Exception as exc:  # noqa: BLE001
                # A failure tearing the socket down must not strand a dead
                # client: the finally below guarantees we discard it so the
                # next tick reconnects cleanly. CancelledError is deliberately
                # left to propagate (not an Exception).
                _LOGGER.debug("Error while closing client (ignored): %s", exc)
            finally:
                self._client = None

    async def async_close(self) -> None:
        await self._reset_client()
