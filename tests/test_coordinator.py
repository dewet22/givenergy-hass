"""Tests for the GivEnergy Local coordinator."""

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from givenergy_modbus.exceptions import (
    PlantTopologyMismatch,
    ReadFailure,
    RefreshFailed,
    RefreshPartiallySucceeded,
)
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.givenergy_local.coordinator import (
    _PARTIAL_LOG_EVERY,
    DETECT_LOSS_RETRIES,
    DETECT_LOSS_RETRY_DELAY,
    PROBE_RETRIES,
    PROBE_TIMEOUT_SECONDS,
    GivEnergyUpdateCoordinator,
    missing_devices,
)


def _caps(**overrides) -> PlantCapabilities:
    """Build a PlantCapabilities for tests; override any field via kwargs."""
    defaults = {
        "device_type": Model.HYBRID,
        "inverter_address": 0x32,
        "meter_addresses": [],
        "lv_battery_addresses": [0x32],
        "bcu_stacks": [],
    }
    return PlantCapabilities(**{**defaults, **overrides})


def _read_failure(addr: int = 0x34) -> ReadFailure:
    """A single failed register read, e.g. one offline battery's input bank."""
    return ReadFailure(
        device_address=addr,
        request_type="ReadInputRegisters",
        base_register=60,
        register_count=60,
    )


def _partial(plant, failures=None) -> RefreshPartiallySucceeded:
    """A partial-poll exception carrying `plant` and the failed reads."""
    failures = failures if failures is not None else [_read_failure()]
    return RefreshPartiallySucceeded(
        "partial poll",
        plant=plant,
        failures=failures,
        cause=ExceptionGroup("reads", [TimeoutError()]),
    )


def _refresh_failed(*causes: BaseException) -> RefreshFailed:
    """A total-poll failure whose ExceptionGroup carries the given causes."""
    return RefreshFailed(
        "link effectively dead",
        failures=[_read_failure()],
        cause=ExceptionGroup("reads", list(causes)),
    )


async def test_first_refresh_connects_and_fetches(hass, mock_client, setup_integration):
    mock_client.connect.assert_called_once()
    mock_client.detect.assert_called_once()
    # Tick 0 is a full refresh → load_config() then refresh(), both threading retries.
    mock_client.load_config.assert_called_once_with(retries=1)
    mock_client.refresh.assert_called_once_with(retries=1)


async def test_reconnects_when_disconnected(hass, mock_client, mock_config_entry):
    mock_client.connected = False

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_client.connect.assert_called_once()


async def test_update_failed_clears_client(hass, mock_plant):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False
        client.connect.side_effect = OSError("connection refused")
        client.close = AsyncMock()
        mock_cls.return_value = client

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator._client is None


async def test_async_close_closes_client(hass, mock_client, setup_integration):
    from custom_components.givenergy_local.const import DOMAIN

    coordinator = hass.data[DOMAIN][setup_integration.entry_id]
    await coordinator.async_close()

    mock_client.close.assert_called_once()
    assert coordinator._client is None


async def test_reset_client_discards_even_if_close_raises(hass):
    """If close() raises while tearing the socket down, the client is still
    discarded so the next tick reconnects rather than reusing a dead client."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    client = AsyncMock()
    client.close = AsyncMock(side_effect=OSError("close failed"))
    coordinator._client = client

    await coordinator._reset_client()  # must NOT raise

    client.close.assert_awaited_once()
    assert coordinator._client is None


async def test_timeout_raises_update_failed(hass):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh.side_effect = TimeoutError()
        mock_cls.return_value = client
        coordinator._client = client

        with pytest.raises(UpdateFailed, match="Timed out"):
            await coordinator._async_update_data()


async def test_timeout_within_tolerance_preserves_client(hass, mock_plant):
    """TimeoutError within tolerance keeps the TCP connection open and serves stale data."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=TimeoutError())
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant  # seed stale data so tolerance path is reached

        result = await coordinator._async_update_data()  # failure 1/3 — within tolerance

        client.close.assert_not_called()
        assert coordinator._client is client
        assert result is mock_plant


async def test_timeout_reaching_tolerance_resets_client(hass, mock_plant):
    """The Nth consecutive timeout (with tolerance=N) resets the client for the next tick."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=2)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=TimeoutError())
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant  # seed stale data

        # First failure — within tolerance, serves stale data.
        result = await coordinator._async_update_data()
        assert result is mock_plant
        client.close.assert_not_called()

        # Second failure — reaches tolerance, resets the client.
        with pytest.raises(UpdateFailed, match="Timed out"):
            await coordinator._async_update_data()

        client.close.assert_called_once()
        assert coordinator._client is None


async def test_detect_timeout_on_reconnect_discards_client(hass, mock_plant):
    """A reconnect whose detect() times out must not retain a capability-less client.

    Otherwise the next tick skips _connect() (the socket still reports connected),
    calls refresh() with capabilities=None, and the library raises PlantNotDetected.
    The half-initialised client must be dropped so the next tick reconnects and
    re-detects cleanly. Regression for #176.
    """
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    coordinator.data = mock_plant  # seed stale data so the tolerance path serves it

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect.side_effect = TimeoutError()
        mock_cls.return_value = client

        # Reconnect tick (_client is None): _connect() → detect() times out.
        result = await coordinator._async_update_data()

        assert result is mock_plant  # last-known served for this tick
        assert coordinator._client is None  # the fix: don't retain a caps-less client
        assert coordinator.consecutive_failures == 1
        client.refresh.assert_not_called()  # never reached refresh on a caps-less client

        # Next tick: detect() now succeeds → clean reconnect, no PlantNotDetected.
        client.detect.side_effect = None
        client.load_config = AsyncMock(return_value=mock_plant)
        client.refresh = AsyncMock(return_value=mock_plant)

        result = await coordinator._async_update_data()

        assert result is mock_plant
        assert coordinator._client is client
        assert coordinator.consecutive_failures == 0


async def test_cancelled_connect_discards_client(hass, mock_plant):
    """A cancellation during _connect() (HA cancelling the attempt) must still
    discard the half-initialised client. CancelledError is a BaseException, not
    Exception, so the cleanup boundary has to catch it too — and re-raise the
    cancellation. Regression for #176 review (Codex/Gemini)."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    coordinator.data = mock_plant

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect.side_effect = asyncio.CancelledError()
        mock_cls.return_value = client

        with pytest.raises(asyncio.CancelledError):
            await coordinator._async_update_data()

    assert coordinator._client is None  # cleaned up despite the cancellation
    client.close.assert_awaited_once()  # _reset_client() ran


async def test_timeout_increments_consecutive_failures(hass):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh.side_effect = TimeoutError()
        mock_cls.return_value = client
        coordinator._client = client

        for expected in range(1, 4):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
            assert coordinator.consecutive_failures == expected


async def test_successful_refresh_resets_failure_count(hass, mock_plant):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh.side_effect = [
            TimeoutError(),
            TimeoutError(),
            mock_plant,
        ]
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(2):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

        assert coordinator.consecutive_failures == 2
        assert coordinator.total_failures == 2

        await coordinator._async_update_data()

        assert coordinator.consecutive_failures == 0
        # total_failures is monotonic — success doesn't reset it.
        assert coordinator.total_failures == 2
        assert coordinator.last_successful_refresh is not None


async def test_total_failures_increments_on_every_failure_type(hass, mock_plant):
    """Each of the three failure paths (UpdateFailed, TimeoutError, generic Exception)
    must increment total_failures."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh.side_effect = [
            TimeoutError(),  # → counted (TimeoutError path)
            ConnectionResetError("peer reset"),  # → counted (generic Exception path)
            mock_plant,  # → success, no count
            TimeoutError(),  # → counted (TimeoutError path)
        ]
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(2):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
        assert coordinator.total_failures == 2

        await coordinator._async_update_data()
        assert coordinator.total_failures == 2  # success doesn't bump it

        coordinator._client = client  # ConnectionReset reset it
        client.connected = True
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert coordinator.total_failures == 3


async def test_reset_and_reconnect_emit_integration_level_logs(hass, mock_plant, caplog):
    """The close-and-reconnect cycle must log under the integration's own logger
    so users don't need to enable the library's logger separately to trace it."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=1)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=TimeoutError())
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant
        # tolerance=1 → first failure already reaches the threshold.

        with caplog.at_level(logging.INFO, logger="custom_components.givenergy_local.coordinator"):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()  # triggers _reset_client

            # Next tick: client is None → _connect() runs and succeeds
            client.connected = True
            client.refresh = AsyncMock(return_value=mock_plant)
            await coordinator._async_update_data()

    messages = [r.getMessage() for r in caplog.records]
    assert any("Closing connection to 192.168.1.1:8899" in m for m in messages)
    assert any("Connected to inverter at 192.168.1.1:8899" in m for m in messages)


async def test_non_timeout_error_closes_client(hass):
    """Non-timeout errors (e.g. connection drop) should reset the client."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh.side_effect = ConnectionResetError("peer reset")
        mock_cls.return_value = client
        coordinator._client = client

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        client.close.assert_called_once()
        assert coordinator._client is None


# ---------------------------------------------------------------------------
# Partial / total refresh outcomes (#125)
# ---------------------------------------------------------------------------


async def test_partial_in_steady_state_serves_partial_and_counts(hass, mock_plant):
    """A steady-state partial poll serves exc.plant, counts as a success, and bumps
    only the partial_failures counter — keeping the good entities live."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    failures = [_read_failure(0x34)]

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_partial(mock_plant, failures))
        mock_cls.return_value = client
        coordinator._client = client  # already connected → not a seed

        result = await coordinator._async_update_data()

        assert result is mock_plant
        assert coordinator.partial_failures == 1
        assert coordinator.consecutive_failures == 0
        assert coordinator.total_failures == 0
        assert coordinator.last_successful_refresh is not None
        assert coordinator.last_partial_failures == failures
        client.close.assert_not_called()
        assert coordinator._client is client


async def test_partial_success_increments_partial_failures_cumulatively(hass, mock_plant):
    """Repeated steady-state partials accumulate on partial_failures."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(3):
            await coordinator._async_update_data()

    assert coordinator.partial_failures == 3
    assert coordinator.consecutive_failures == 0
    assert coordinator.total_failures == 0


def _comms_plant(
    crc=None, reject=None, held=None, retry=None, cold=None, system_time=None
) -> SimpleNamespace:
    """A minimal plant stub carrying the library's comms-quality counters."""
    return SimpleNamespace(
        inverter=SimpleNamespace(system_time=system_time),
        crc_failure_count=crc if crc is not None else {},
        splice_reject_count=reject if reject is not None else {},
        splice_held_count=held if held is not None else {},
        retry_count=retry if retry is not None else {},
        cold_start_held_count=cold if cold is not None else {},
    )


async def test_comms_counters_accumulate_per_device(hass):
    """Each poll mirrors the library's per-device counters, accruing only deltas."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    coordinator._accumulate_comms_counters(
        _comms_plant(
            crc={0x32: 2, 0x33: 1},
            reject={0x33: 4},
            held={0x70: 5},
            retry={0x33: 6},
            cold={0x32: 2},
        )
    )
    assert coordinator.crc_failures_by_device == {0x32: 2, 0x33: 1}
    assert coordinator.splice_rejections_by_device == {0x33: 4}
    assert coordinator.splice_holds_by_device == {0x70: 5}
    assert coordinator.read_retries_by_device == {0x33: 6}
    assert coordinator.cold_start_held_by_device == {0x32: 2}

    # A later poll with higher cumulative values accrues only the delta.
    coordinator._accumulate_comms_counters(
        _comms_plant(
            crc={0x32: 5, 0x33: 1},
            reject={0x33: 4},
            held={0x70: 9},
            retry={0x33: 10},
            cold={0x32: 5},
        )
    )
    assert coordinator.crc_failures_by_device == {0x32: 5, 0x33: 1}
    assert coordinator.splice_rejections_by_device == {0x33: 4}
    assert coordinator.splice_holds_by_device == {0x70: 9}
    assert coordinator.read_retries_by_device == {0x33: 10}
    assert coordinator.cold_start_held_by_device == {0x32: 5}


async def test_comms_counters_monotonic_across_plant_reset(hass):
    """A reconnect resets the library counter; our mirror stays monotonic."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    coordinator._accumulate_comms_counters(_comms_plant(crc={0x32: 5}))
    # Reconnect → fresh library Plant, counter resets to a lower value.
    coordinator._accumulate_comms_counters(_comms_plant(crc={0x32: 2}))
    assert coordinator.crc_failures_by_device == {0x32: 7}  # 5 banked + 2 new
    coordinator._accumulate_comms_counters(_comms_plant(crc={0x32: 4}))
    assert coordinator.crc_failures_by_device == {0x32: 9}  # + (4 - 2)


async def test_comms_counters_absent_leaves_zero(hass):
    """An older modbus (attrs absent) or a MagicMock plant no-ops cleanly."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    coordinator._accumulate_comms_counters(SimpleNamespace())  # attrs absent
    coordinator._accumulate_comms_counters(MagicMock())  # auto-attrs, not dicts
    assert coordinator.crc_failures_by_device == {}
    assert coordinator.splice_rejections_by_device == {}
    assert coordinator.splice_holds_by_device == {}
    assert coordinator.read_retries_by_device == {}
    assert coordinator.cold_start_held_by_device == {}


async def test_mark_success_accumulates_comms_counters(hass):
    """The accumulation is wired into _mark_success (every data-bearing poll)."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    coordinator._mark_success(_comms_plant(crc={0x32: 3}, system_time=datetime.now()))
    assert coordinator.crc_failures_by_device == {0x32: 3}
    assert coordinator.last_successful_refresh is not None


async def test_reconnect_clears_comms_baseline(hass):
    """A reconnect builds a fresh Client/Plant with zeroed counters, so _connect
    must drop the last-seen baselines (else the next poll diffs against a stale
    pre-reconnect value and undercounts)."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    coordinator._comms_last_seen = {"crc_failure_count": {0x32: 9}}

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False
        client.connect.side_effect = OSError("connection refused")
        mock_cls.return_value = client
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    # The clear runs at Client construction, before connect() is even attempted.
    assert coordinator._comms_last_seen == {}


async def test_comms_counters_full_count_after_baseline_reset(hass):
    """With the baseline cleared on reconnect, a counter that climbs back past
    its pre-reconnect value is counted in full, not diffed against the stale
    baseline (Gemini #197)."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    coordinator._accumulate_comms_counters(_comms_plant(crc={0x32: 2}))
    assert coordinator.crc_failures_by_device == {0x32: 2}

    # Reconnect: _connect() clears the baselines (fresh Plant, counters zeroed).
    coordinator._comms_last_seen.clear()

    # 3 errors before the first post-reconnect poll → current 3 (>= old last 2).
    coordinator._accumulate_comms_counters(_comms_plant(crc={0x32: 3}))
    assert coordinator.crc_failures_by_device == {0x32: 5}  # 2 + 3, not 2 + 1


async def test_partial_log_throttled_and_resets(hass, mock_plant, caplog):
    """First partial of a run warns; subsequent ones drop to debug; a clean poll
    ends the run so the next partial warns afresh. (partial_failures unaffected.)"""
    caplog.set_level(logging.DEBUG, logger="custom_components.givenergy_local.coordinator")
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    def partial_warnings() -> list:
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Partial refresh" in r.getMessage()
        ]

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client

        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        for _ in range(3):
            await coordinator._async_update_data()
        assert len(partial_warnings()) == 1  # only the first of the run warned
        assert coordinator.partial_failures == 3  # counter still bumps every poll

        client.refresh = AsyncMock(return_value=mock_plant)
        await coordinator._async_update_data()  # clean poll ends the run
        assert coordinator._consecutive_partials == 0

        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        await coordinator._async_update_data()
        assert len(partial_warnings()) == 2  # next run warns afresh


async def test_partial_log_periodic_rewarn(hass, mock_plant, caplog):
    """A sustained partial run re-warns every _PARTIAL_LOG_EVERY polls."""
    caplog.set_level(logging.DEBUG, logger="custom_components.givenergy_local.coordinator")
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)
    exc = _partial(mock_plant)

    for _ in range(_PARTIAL_LOG_EVERY):
        coordinator._record_partial(exc)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # poll 1 (first) + poll _PARTIAL_LOG_EVERY (periodic)
    assert coordinator.partial_failures == _PARTIAL_LOG_EVERY


async def test_clean_poll_retains_partial_detail(hass, mock_plant):
    """After a partial, a later clean poll RETAINS last_partial_failures and
    last_partial_at so the diagnostic can still name the bank that dropped after
    an intermittent failure recovers (#176). The throttle run still ends and the
    cumulative counter is unaffected."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=[_partial(mock_plant), mock_plant])
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()  # partial — detail populated
        assert coordinator.last_partial_failures
        assert coordinator.last_partial_at is not None
        await coordinator._async_update_data()  # clean — detail retained

    assert coordinator.last_partial_failures  # retained past the clean poll
    assert coordinator.last_partial_at is not None
    assert coordinator._consecutive_partials == 0  # throttle run ended
    assert coordinator.partial_failures == 1  # counter is cumulative, retained


async def test_partial_on_cold_seed_with_identity_serves(hass, mock_plant):
    """A partial on a cold seed whose inverter identified itself (serial present) is
    SERVED, so the integration loads — the failed reads' entities go unavailable
    rather than the whole integration looping in ConfigEntryNotReady."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        mock_cls.return_value = client
        # _client is None → reconnecting=True, coordinator.data is None (cold),
        # mock_plant.inverter_serial_number is set → usable.

        result = await coordinator._async_update_data()

    assert result is mock_plant  # served the partial, not a fail-hard
    assert coordinator.partial_failures == 1
    assert coordinator.consecutive_failures == 0  # served counts as success
    assert coordinator.last_partial_failures  # set → blocks the cold-start persist
    assert coordinator._client is client  # kept


async def test_partial_on_cold_seed_without_identity_raises(hass, mock_plant):
    """A cold-seed partial too sparse to set up (the inverter didn't even identify
    itself) still fails so HA retries setup (→ ConfigEntryNotReady), and the message
    names the failed register block."""
    mock_plant.inverter_serial_number = None  # not identifiable → unusable seed
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        mock_cls.return_value = client

        with pytest.raises(UpdateFailed, match=r"on device 0x34"):
            await coordinator._async_update_data()

    # Fail-hard, not counted as a partial; client reset for a clean retry.
    assert coordinator.partial_failures == 0
    assert coordinator.total_failures == 1
    assert coordinator.consecutive_failures == 1
    assert coordinator._client is None


async def test_partial_on_inprocess_reconnect_seed_serves_last_known(hass, mock_plant):
    """A partial on an in-process reconnect seed serves the pre-disconnect data
    (within the tolerance window) rather than the partial plant."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)
    prior_data = mock_plant

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False  # forces a reconnect (seed)
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_partial(mock_plant))
        mock_cls.return_value = client
        coordinator.data = prior_data  # pre-disconnect snapshot available

        result = await coordinator._async_update_data()

    assert result is prior_data  # served last-known, not exc.plant
    assert coordinator.partial_failures == 0
    assert coordinator.total_failures == 1
    assert coordinator._client is client  # kept — transient


async def test_refresh_failed_timeout_cause_within_tolerance_serves_stale(hass, mock_plant):
    """A total RefreshFailed whose causes are all timeouts is treated as transient:
    serve last-known data within the tolerance window."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_refresh_failed(TimeoutError(), TimeoutError()))
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant

        result = await coordinator._async_update_data()

    assert result is mock_plant
    assert coordinator._client is client
    client.close.assert_not_called()
    assert coordinator.total_failures == 1


async def test_refresh_failed_timeout_cause_reaching_tolerance_resets(hass, mock_plant):
    """Timeout-driven RefreshFailed escalates to UpdateFailed + reset at the threshold."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=2)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_refresh_failed(TimeoutError()))
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant

        result = await coordinator._async_update_data()  # 1/2 — serves stale
        assert result is mock_plant
        client.close.assert_not_called()

        with pytest.raises(UpdateFailed, match="Timed out"):
            await coordinator._async_update_data()  # 2/2 — resets

        client.close.assert_called_once()
        assert coordinator._client is None


async def test_refresh_failed_bare_timeout_cause_serves_stale(hass, mock_plant):
    """Defensive: a RefreshFailed whose cause is a bare TimeoutError (not wrapped in
    an ExceptionGroup) is still recognised as a timeout and served within tolerance."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)
    bare = RefreshFailed("link dead", failures=[_read_failure()], cause=TimeoutError())

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=bare)
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant

        result = await coordinator._async_update_data()

    assert result is mock_plant
    assert coordinator._client is client


async def test_refresh_failed_nontimeout_cause_resets_immediately(hass, mock_plant):
    """A RefreshFailed with any non-timeout cause resets the client and fails at once,
    even when last-known data is available."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=_refresh_failed(ConnectionResetError("peer reset")))
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant  # present, but must not be served

        with pytest.raises(UpdateFailed, match="Error communicating"):
            await coordinator._async_update_data()

        client.close.assert_called_once()
        assert coordinator._client is None
        assert coordinator.total_failures == 1


async def test_refresh_failed_mixed_group_cause_resets_immediately(hass, mock_plant):
    """A RefreshFailed whose ExceptionGroup mixes a timeout with a non-timeout is
    NOT treated as a transient timeout: it resets the client and fails at once,
    rather than serving stale within the tolerance window."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, timeout_tolerance=3)
    mixed = _refresh_failed(TimeoutError(), ConnectionResetError("peer reset"))

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(side_effect=mixed)
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant  # present, but must not be served

        with pytest.raises(UpdateFailed, match="Error communicating"):
            await coordinator._async_update_data()

        client.close.assert_awaited_once()
        assert coordinator._client is None
        assert coordinator.total_failures == 1


# ---------------------------------------------------------------------------
# Active / passive refresh cadence
# ---------------------------------------------------------------------------


async def test_passive_mode_initial_connect_does_full_refresh(hass, mock_plant):
    """Even in passive mode the first connect must seed the cache with a full refresh."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.load_config.assert_called_once_with(retries=1)
    client.refresh.assert_called_once_with(retries=1)


async def test_passive_mode_skips_refresh_on_subsequent_ticks(hass, mock_plant):
    """After the initial connect, passive mode must not send any Modbus requests."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client  # already connected

        from datetime import timedelta

        base = datetime(2026, 5, 10, 12, 0, 0)
        for tick in range(3):
            mock_plant.inverter.system_time = base + timedelta(seconds=tick * 30)
            await coordinator._async_update_data()

    # No wire traffic — the client was already connected.
    client.refresh.assert_not_called()
    client.load_config.assert_not_called()


async def test_passive_mode_reconnect_does_full_refresh(hass, mock_plant):
    """If the connection drops in passive mode, reconnecting must re-seed the cache."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False  # simulate a dropped connection
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.load_config.assert_called_once_with(retries=1)
    client.refresh.assert_called_once_with(retries=1)


async def test_retries_forwarded_to_refresh_active(hass, mock_plant):
    """Active-mode ticks must thread the configured retries count to the primitives."""
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, passive=False, retries=2
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()

    client.load_config.assert_called_once_with(retries=2)
    client.refresh.assert_called_once_with(retries=2)


async def test_retries_forwarded_to_refresh_passive_reconnect(hass, mock_plant):
    """Passive-mode reconnect (the only path that hits the wire) must also forward retries."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True, retries=3)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False  # forces reconnect
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.load_config.assert_called_once_with(retries=3)
    client.refresh.assert_called_once_with(retries=3)


async def test_active_mode_always_refreshes(hass, mock_plant):
    """In active (default) mode every tick issues a refresh() request."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(3):
            await coordinator._async_update_data()

    assert client.refresh.call_count == 3


async def test_active_mode_first_tick_is_full_refresh(hass, mock_plant):
    """Tick 0 must always be a full refresh (load_config + refresh) regardless of interval."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()

    client.load_config.assert_called_once_with(retries=1)
    client.refresh.assert_called_once_with(retries=1)


async def test_active_mode_intermediate_ticks_are_partial(hass, mock_plant):
    """Ticks 1 … (n-1) must skip load_config (input registers only)."""
    # scan_interval=30 → _full_refresh_every = round(300/30) = 10
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(3):  # ticks 0, 1, 2
            await coordinator._async_update_data()

    # load_config only on tick 0; refresh on every tick.
    assert client.load_config.call_count == 1
    assert client.refresh.call_count == 3


async def test_active_mode_nth_tick_is_full_refresh(hass, mock_plant):
    """Every _full_refresh_every ticks a full refresh (load_config) must be issued again."""
    # scan_interval=30 → _full_refresh_every = 10; tick 10 is the next full refresh
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(11):  # ticks 0-10
            await coordinator._async_update_data()

    # load_config on ticks 0 and 10; refresh on all 11.
    assert client.load_config.call_count == 2
    assert client.refresh.call_count == 11


async def test_active_mode_reconnect_resets_refresh_cycle(hass, mock_plant):
    """After a reconnect the full-refresh cycle must restart from tick 0."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        # Advance a few ticks so _active_tick > 0 (load_config fired once, on tick 0)
        for _ in range(3):
            await coordinator._async_update_data()
        assert client.load_config.call_count == 1

        # Simulate a reconnect by resetting the client
        coordinator._client = None

        await coordinator._async_update_data()

    # The post-reconnect call is tick 0 of a new cycle → load_config fires again.
    assert client.load_config.call_count == 2


async def test_passive_stale_cache_raises_after_two_unchanged_ticks(hass, mock_plant):
    """Cache is only considered stale after two consecutive unchanged ticks."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)
    fixed_time = datetime(2026, 5, 10, 12, 0, 0)
    mock_plant.inverter.system_time = fixed_time

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()  # tick 1: seeds _last_inverter_time
        await coordinator._async_update_data()  # tick 2: first unchanged — tolerated
        with pytest.raises(UpdateFailed, match="2 consecutive ticks"):
            await coordinator._async_update_data()  # tick 3: second unchanged — stale

    assert coordinator.consecutive_failures == 1


async def test_passive_one_unchanged_tick_is_tolerated(hass, mock_plant):
    """A single unchanged tick is allowed before the stale error is raised."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)
    fixed_time = datetime(2026, 5, 10, 12, 0, 0)
    mock_plant.inverter.system_time = fixed_time

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()  # seed
        await coordinator._async_update_data()  # first unchanged — must not raise

    assert coordinator.consecutive_failures == 0


async def test_passive_advancing_system_time_succeeds(hass, mock_plant):
    """If system_time advances the cache is live and no error is raised."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client

        mock_plant.inverter.system_time = datetime(2026, 5, 10, 12, 0, 0)
        await coordinator._async_update_data()

        mock_plant.inverter.system_time = datetime(2026, 5, 10, 12, 0, 30)
        await coordinator._async_update_data()

    assert coordinator.consecutive_failures == 0


async def test_passive_reconnect_resets_stale_detection(hass, mock_plant):
    """Reconnecting clears _last_inverter_time so the first passive tick never fires stale."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)
    fixed_time = datetime(2026, 5, 10, 12, 0, 0)
    mock_plant.inverter.system_time = fixed_time
    coordinator._last_inverter_time = fixed_time  # same as what the plant will return

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh = AsyncMock(return_value=mock_plant)
        client.load_config = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        # _client is None → reconnecting=True → _last_inverter_time is reset before the check

        await coordinator._async_update_data()

    assert coordinator.consecutive_failures == 0


async def test_passive_none_system_time_skips_stale_check(hass, mock_plant):
    """If system_time is None (register not yet populated) the stale check is skipped."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)
    mock_plant.inverter.system_time = None

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()
        await coordinator._async_update_data()  # would raise if check wasn't skipped

    assert coordinator.consecutive_failures == 0


# ---------------------------------------------------------------------------
# PlantCapabilities cache integration (issue #48)
# ---------------------------------------------------------------------------


async def test_connect_passes_prior_capabilities_to_detect(hass, mock_plant):
    """When seeded with a prior, _connect() must thread it through detect(prior=)."""
    prior = _caps()
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, prior_capabilities=prior
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock()
        mock_cls.return_value = client

        await coordinator._connect()

    client.detect.assert_awaited_once_with(
        prior=prior, probe_timeout=PROBE_TIMEOUT_SECONDS, probe_retries=PROBE_RETRIES
    )


async def test_connect_passes_none_prior_when_no_cache(hass, mock_plant):
    """No cache means a cold detect — explicitly prior=None, not a missing kwarg."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock()
        mock_cls.return_value = client

        await coordinator._connect()

    client.detect.assert_awaited_once_with(
        prior=None, probe_timeout=PROBE_TIMEOUT_SECONDS, probe_retries=PROBE_RETRIES
    )


async def test_topology_mismatch_accepts_actual_and_invokes_callback(hass, mock_plant):
    """PlantTopologyMismatch: assign exc.actual to plant, update prior, call back, no raise."""
    prior = _caps(lv_battery_addresses=[0x32])
    actual = _caps(lv_battery_addresses=[0x32, 0x33])
    mismatch = PlantTopologyMismatch("topology changed", prior=prior, actual=actual)
    callback = AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=callback,
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant  # the assignment in _connect targets client.plant.capabilities
        client.detect = AsyncMock(side_effect=mismatch)
        mock_cls.return_value = client

        await coordinator._connect()  # must NOT raise

    # Capabilities accepted on the live plant so this tick's refresh dispatches correctly.
    assert client.plant.capabilities is actual
    # Coordinator's own prior is updated so any in-process reconnect uses the new topology.
    assert coordinator._prior_capabilities is actual
    # Callback fired with the new capabilities — the wiring in __init__.py uses
    # this to persist and schedule a reload.
    callback.assert_awaited_once_with(actual)


async def test_topology_mismatch_without_callback_still_accepts_actual(hass, mock_plant):
    """A coordinator constructed without a callback (e.g. in unit tests) must not raise."""
    prior = _caps()
    actual = _caps(lv_battery_addresses=[0x32, 0x33])
    mismatch = PlantTopologyMismatch("changed", prior=prior, actual=actual)
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, prior_capabilities=prior
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=mismatch)
        mock_cls.return_value = client

        await coordinator._connect()

    assert client.plant.capabilities is actual
    assert coordinator._prior_capabilities is actual


def test_missing_devices_classification():
    """missing_devices() flags losses (battery/meter/HV) but not adds or type changes."""
    base = _caps(lv_battery_addresses=[0x32, 0x33])
    # Battery dropped.
    assert missing_devices(base, _caps(lv_battery_addresses=[0x32])) == ["battery at 0x33"]
    # Meter dropped.
    assert missing_devices(_caps(meter_addresses=[0x01]), _caps(meter_addresses=[])) == [
        "meter at 0x01"
    ]
    # HV stack offset removed.
    assert missing_devices(_caps(bcu_stacks=[(0, 4)]), _caps(bcu_stacks=[])) == ["HV stack at 0x70"]
    # HV stack module count shrank.
    assert missing_devices(_caps(bcu_stacks=[(0, 4)]), _caps(bcu_stacks=[(0, 2)])) == [
        "HV stack at 0x70 (2 of 4 modules)"
    ]
    # AIO battery module dropped (#148).
    assert missing_devices(
        _caps(aio_battery_module_addresses=[0x50, 0x51]),
        _caps(aio_battery_module_addresses=[0x50]),
    ) == ["AIO battery module at 0x51"]
    # An add is not a loss.
    assert missing_devices(_caps(lv_battery_addresses=[0x32]), base) == []
    # An AIO module add is not a loss either.
    assert (
        missing_devices(
            _caps(aio_battery_module_addresses=[0x50]),
            _caps(aio_battery_module_addresses=[0x50, 0x51]),
        )
        == []
    )
    # A device_type change is not a loss (the routine reload path handles it).
    assert missing_devices(_caps(), _caps(device_type=Model.AC)) == []
    # No prior (cold start) is not a loss.
    assert missing_devices(None, base) == []


async def test_loss_retried_then_heals(hass, mock_plant):
    """A loss that clears on retry: full prior kept, healed callback, no loss callback."""
    prior = _caps(lv_battery_addresses=[0x32, 0x33])
    actual = _caps(lv_battery_addresses=[0x32])
    mismatch = PlantTopologyMismatch("battery missing", prior=prior, actual=actual)
    on_missing, on_changed, on_healed = AsyncMock(), AsyncMock(), AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=on_changed,
        on_devices_missing=on_missing,
        on_topology_healed=on_healed,
    )

    with (
        patch("custom_components.givenergy_local.coordinator.Client") as mock_cls,
        patch(
            "custom_components.givenergy_local.coordinator.asyncio.sleep", AsyncMock()
        ) as mock_sleep,
    ):
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=[mismatch, None])  # heals on retry
        mock_cls.return_value = client

        await coordinator._connect()  # must NOT raise

    assert client.detect.await_count == 2  # initial + one healing retry
    mock_sleep.assert_awaited_once_with(DETECT_LOSS_RETRY_DELAY)
    on_missing.assert_not_awaited()
    on_changed.assert_not_awaited()
    on_healed.assert_awaited_once()
    assert coordinator._prior_capabilities is prior  # full prior never overwritten


async def test_healed_callback_failure_does_not_break_connection(hass, mock_plant):
    """A crash inside the topology-healed callback must not discard the freshly
    connected client or fail the connect — the link is up, so we log and carry on."""
    on_healed = AsyncMock(side_effect=RuntimeError("reload failed"))
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, on_topology_healed=on_healed
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock()  # clean detect → topology confirmed, callback fires
        mock_cls.return_value = client

        await coordinator._connect()  # must NOT raise despite the callback blowing up

    on_healed.assert_awaited_once()
    assert coordinator._client is client  # connection kept, not discarded


async def test_persistent_loss_invokes_on_devices_missing(hass, mock_plant):
    """A loss surviving retries: loss callback, prior kept, reduced caps for the tick."""
    prior = _caps(lv_battery_addresses=[0x32, 0x33])
    actual = _caps(lv_battery_addresses=[0x32])
    mismatch = PlantTopologyMismatch("battery missing", prior=prior, actual=actual)
    on_missing, on_changed, on_healed = AsyncMock(), AsyncMock(), AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=on_changed,
        on_devices_missing=on_missing,
        on_topology_healed=on_healed,
    )

    with (
        patch("custom_components.givenergy_local.coordinator.Client") as mock_cls,
        patch("custom_components.givenergy_local.coordinator.asyncio.sleep", AsyncMock()),
    ):
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=mismatch)  # never recovers
        mock_cls.return_value = client

        await coordinator._connect()  # must NOT raise

    assert client.detect.await_count == 1 + DETECT_LOSS_RETRIES
    on_missing.assert_awaited_once_with(prior, actual)
    on_changed.assert_not_awaited()  # not a routine change → no persist/reload
    on_healed.assert_not_awaited()
    assert coordinator._prior_capabilities is prior  # loss NOT baked in
    assert client.plant.capabilities is actual  # reduced topology served this tick
    assert coordinator._schedule_reconnect  # forces a reconnect on the next poll


async def test_unload_mid_loss_retry_does_not_crash(hass, mock_plant):
    """An unload during the loss-retry sleeps discards the client (async_close);
    when the retry loop resumes the whole refresh must bail out quietly — no
    AttributeError on the None client, no topology callbacks for a resolution
    that was abandoned mid-flight, and no UpdateFailed (a scary ERROR in the HA
    log) for what is a routine teardown: the tick serves last-known data
    instead (Gemini review on #159)."""
    prior = _caps(lv_battery_addresses=[0x32, 0x33])
    actual = _caps(lv_battery_addresses=[0x32])
    mismatch = PlantTopologyMismatch("battery missing", prior=prior, actual=actual)
    on_missing, on_changed, on_healed = AsyncMock(), AsyncMock(), AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=on_changed,
        on_devices_missing=on_missing,
        on_topology_healed=on_healed,
    )

    async def _unload_during_sleep(_delay):
        # Simulates async_unload_entry running while this refresh sleeps.
        await coordinator.async_close()

    with (
        patch("custom_components.givenergy_local.coordinator.Client") as mock_cls,
        patch(
            "custom_components.givenergy_local.coordinator.asyncio.sleep",
            AsyncMock(side_effect=_unload_during_sleep),
        ),
    ):
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=mismatch)  # loss on the initial detect
        mock_cls.return_value = client

        # The full refresh path: must neither raise UpdateFailed nor crash —
        # the post-_connect guard serves last-known data (None here) quietly.
        result = await coordinator._async_update_data()

    assert result is coordinator.data
    assert coordinator._client is None  # closed stays closed
    assert coordinator.consecutive_failures == 0  # teardown is not a failure
    on_missing.assert_not_awaited()
    on_changed.assert_not_awaited()
    on_healed.assert_not_awaited()
    assert coordinator._prior_capabilities is prior  # nothing baked in


async def test_loss_reconnect_respects_cooldown(hass, mock_plant):
    """_schedule_reconnect within cooldown: no reset, normal poll continues."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        mock_cls.return_value = client
        coordinator._client = client
        coordinator._schedule_reconnect = True
        coordinator._loss_redetect_after = float("inf")  # cooldown not yet expired

        await coordinator._async_update_data()

    # Client must NOT have been reset — reconnect was deferred by cooldown.
    client.close.assert_not_awaited()
    assert coordinator._client is client
    # Flag remains set so the reconnect fires once the cooldown expires.
    assert coordinator._schedule_reconnect


async def test_device_type_change_uses_topology_changed_path(hass, mock_plant):
    """A device_type change is routine (not a loss): accept, update prior, reload."""
    prior = _caps()
    actual = _caps(device_type=Model.AC)
    mismatch = PlantTopologyMismatch("type changed", prior=prior, actual=actual)
    on_missing, on_changed = AsyncMock(), AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=on_changed,
        on_devices_missing=on_missing,
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=mismatch)
        mock_cls.return_value = client

        await coordinator._connect()

    client.detect.assert_awaited_once()  # no retries for a non-loss
    on_changed.assert_awaited_once_with(actual)
    on_missing.assert_not_awaited()
    assert coordinator._prior_capabilities is actual
    assert client.plant.capabilities is actual


async def test_loss_retry_surfacing_add_falls_through(hass, mock_plant):
    """A loss whose retry reveals a non-loss change (an add) takes the routine path."""
    prior = _caps(lv_battery_addresses=[0x32, 0x33])
    loss_actual = _caps(lv_battery_addresses=[0x32])
    add_actual = _caps(lv_battery_addresses=[0x32, 0x33, 0x34])
    loss = PlantTopologyMismatch("battery missing", prior=prior, actual=loss_actual)
    add = PlantTopologyMismatch("battery added", prior=prior, actual=add_actual)
    on_missing, on_changed, on_healed = AsyncMock(), AsyncMock(), AsyncMock()
    coordinator = GivEnergyUpdateCoordinator(
        hass,
        "192.168.1.1",
        8899,
        30,
        prior_capabilities=prior,
        on_topology_changed=on_changed,
        on_devices_missing=on_missing,
        on_topology_healed=on_healed,
    )

    with (
        patch("custom_components.givenergy_local.coordinator.Client") as mock_cls,
        patch("custom_components.givenergy_local.coordinator.asyncio.sleep", AsyncMock()),
    ):
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.detect = AsyncMock(side_effect=[loss, add])
        mock_cls.return_value = client

        await coordinator._connect()

    assert client.detect.await_count == 2
    on_changed.assert_awaited_once_with(add_actual)  # routine path with the retry's actual
    on_missing.assert_not_awaited()
    on_healed.assert_not_awaited()
    assert coordinator._prior_capabilities is add_actual


async def test_default_passes_only_host_port_to_client(hass):
    """Backward-compat: with no experimental flags, Client is built with host/port only."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False
        client.connect.side_effect = OSError("connection refused")  # stop after construction
        client.close = AsyncMock()
        mock_cls.return_value = client
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert mock_cls.call_args.kwargs == {"host": "192.168.1.1", "port": 8899}


async def test_experimental_kwargs_forwarded_to_client(hass):
    """Resolved experimental kwargs are splatted into the Client(...) construction."""
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, experimental_client_kwargs={"demo_kwarg": 5.0}
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False
        client.connect.side_effect = OSError("connection refused")
        client.close = AsyncMock()
        mock_cls.return_value = client
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert mock_cls.call_args.kwargs == {
        "host": "192.168.1.1",
        "port": 8899,
        "demo_kwarg": 5.0,
    }
