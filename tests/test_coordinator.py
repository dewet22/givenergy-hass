"""Tests for the GivEnergy Local coordinator."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.givenergy_local.coordinator import GivEnergyUpdateCoordinator


async def test_first_refresh_connects_and_fetches(hass, mock_client, setup_integration):
    mock_client.connect.assert_called_once()
    mock_client.refresh_plant.assert_called_once_with(
        full_refresh=True, max_batteries=1
    )


async def test_reconnects_when_disconnected(hass, mock_client, mock_config_entry):
    mock_client.connected = False

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Client was disconnected so connect() should have been called
    mock_client.connect.assert_called_once()


async def test_update_failed_clears_client(hass, mock_plant):
    coordinator = GivEnergyUpdateCoordinator(hass, "192.168.1.1", 8899, 30, 1)

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
