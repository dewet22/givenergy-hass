"""Tests for the GivEnergy Local coordinator."""

import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.givenergy_local.coordinator import GivEnergyUpdateCoordinator


async def test_first_refresh_connects_and_fetches(hass, mock_client, setup_integration):
    mock_client.connect.assert_called_once()
    mock_client.detect.assert_called_once()
    mock_client.refresh_plant.assert_called_once_with(full_refresh=True, retries=1)


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


async def test_timeout_raises_update_failed(hass):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh_plant.side_effect = TimeoutError()
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
        client.refresh_plant = AsyncMock(side_effect=TimeoutError())
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
        client.refresh_plant = AsyncMock(side_effect=TimeoutError())
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


async def test_timeout_increments_consecutive_failures(hass):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.refresh_plant.side_effect = TimeoutError()
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
        client.refresh_plant.side_effect = [
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
        client.refresh_plant.side_effect = [
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
        client.refresh_plant = AsyncMock(side_effect=TimeoutError())
        mock_cls.return_value = client
        coordinator._client = client
        coordinator.data = mock_plant
        # tolerance=1 → first failure already reaches the threshold.

        with caplog.at_level(logging.INFO, logger="custom_components.givenergy_local.coordinator"):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()  # triggers _reset_client

            # Next tick: client is None → _connect() runs and succeeds
            client.connected = True
            client.refresh_plant = AsyncMock(return_value=mock_plant)
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
        client.refresh_plant.side_effect = ConnectionResetError("peer reset")
        mock_cls.return_value = client
        coordinator._client = client

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        client.close.assert_called_once()
        assert coordinator._client is None


async def test_passive_mode_initial_connect_does_full_refresh(hass, mock_plant):
    """Even in passive mode the first connect must seed the cache with a full refresh."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.refresh_plant.assert_called_once_with(full_refresh=True, retries=1)


async def test_passive_mode_skips_refresh_on_subsequent_ticks(hass, mock_plant):
    """After the initial connect, passive mode must not send any Modbus requests."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client  # already connected

        from datetime import timedelta

        base = datetime(2026, 5, 10, 12, 0, 0)
        for tick in range(3):
            mock_plant.inverter.system_time = base + timedelta(seconds=tick * 30)
            await coordinator._async_update_data()

    # refresh_plant must never be called — client was already connected
    client.refresh_plant.assert_not_called()


async def test_passive_mode_reconnect_does_full_refresh(hass, mock_plant):
    """If the connection drops in passive mode, reconnecting must re-seed the cache."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False  # simulate a dropped connection
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.refresh_plant.assert_called_once_with(full_refresh=True, retries=1)


async def test_retries_forwarded_to_refresh_plant_active(hass, mock_plant):
    """Active-mode ticks must thread the configured retries count to refresh_plant()."""
    coordinator = GivEnergyUpdateCoordinator(
        hass, "192.168.1.1", 8899, 30, passive=False, retries=2
    )

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()

    client.refresh_plant.assert_called_once_with(full_refresh=True, retries=2)


async def test_retries_forwarded_to_refresh_plant_passive_reconnect(hass, mock_plant):
    """Passive-mode reconnect (the only path that hits the wire) must also forward retries."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=True, retries=3)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = False  # forces reconnect
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client

        await coordinator._async_update_data()

    client.refresh_plant.assert_called_once_with(full_refresh=True, retries=3)


async def test_active_mode_always_refreshes(hass, mock_plant):
    """In active (default) mode every tick issues a refresh_plant request."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(3):
            await coordinator._async_update_data()

    assert client.refresh_plant.call_count == 3


async def test_active_mode_first_tick_is_full_refresh(hass, mock_plant):
    """Tick 0 must always be a full refresh regardless of interval."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        await coordinator._async_update_data()

    client.refresh_plant.assert_called_once_with(full_refresh=True, retries=1)


async def test_active_mode_intermediate_ticks_are_partial(hass, mock_plant):
    """Ticks 1 … (n-1) must use full_refresh=False."""
    # scan_interval=30 → _full_refresh_every = round(300/30) = 10
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(3):  # ticks 0, 1, 2
            await coordinator._async_update_data()

    calls = client.refresh_plant.call_args_list
    assert calls[0].kwargs["full_refresh"] is True  # tick 0 — full
    assert calls[1].kwargs["full_refresh"] is False  # tick 1 — partial
    assert calls[2].kwargs["full_refresh"] is False  # tick 2 — partial


async def test_active_mode_nth_tick_is_full_refresh(hass, mock_plant):
    """Every _full_refresh_every ticks a full refresh must be issued again."""
    # scan_interval=30 → _full_refresh_every = 10; tick 10 is the next full refresh
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        for _ in range(11):  # ticks 0–10
            await coordinator._async_update_data()

    calls = client.refresh_plant.call_args_list
    assert calls[0].kwargs["full_refresh"] is True  # tick 0
    assert calls[10].kwargs["full_refresh"] is True  # tick 10
    for i in range(1, 10):
        assert calls[i].kwargs["full_refresh"] is False


async def test_active_mode_reconnect_resets_refresh_cycle(hass, mock_plant):
    """After a reconnect the full-refresh cycle must restart from tick 0."""
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, passive=False)

    with patch("custom_components.givenergy_local.coordinator.Client") as mock_cls:
        client = AsyncMock()
        client.connected = True
        client.plant = mock_plant
        client.refresh_plant = AsyncMock(return_value=mock_plant)
        mock_cls.return_value = client
        coordinator._client = client

        # Advance a few ticks so _active_tick > 0
        for _ in range(3):
            await coordinator._async_update_data()

        # Simulate a reconnect by resetting the client
        coordinator._client = None

        await coordinator._async_update_data()

    # The post-reconnect call should be a full refresh (tick 0 of new cycle)
    last_call = client.refresh_plant.call_args_list[-1]
    assert last_call.kwargs["full_refresh"] is True


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
