"""Custom integration for local control of GivEnergy energy systems.

For more details about this integration, please refer to
https://github.com/dewet22/givenergy-hass
"""
from __future__ import annotations

from datetime import timedelta

from givenergy_modbus.client.client import Client
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_REFRESH_INTERVAL, CONF_FULL_REFRESH_INTERVAL, LOGGER
from .coordinator import GivEnergyCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})
    client = Client(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])

    hass.data[DOMAIN][entry.entry_id] = coordinator = GivEnergyCoordinator(
        hass=hass,
        client=client,
        update_interval=timedelta(seconds=entry.data.get('CONF_REFRESH_INTERVAL', 10)),
        full_refresh_interval=timedelta(minutes=entry.data.get('CONF_FULL_REFRESH_INTERVAL', 60)),
    )
    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    await hass.data[DOMAIN].pop(entry.entry_id).close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
