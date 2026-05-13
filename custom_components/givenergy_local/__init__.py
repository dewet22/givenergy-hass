from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MAX_BATTERIES,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    DEFAULT_MAX_BATTERIES,
    DEFAULT_PASSIVE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT_TOLERANCE,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GivEnergyUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GivEnergyUpdateCoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        max_batteries=entry.data.get(CONF_MAX_BATTERIES, DEFAULT_MAX_BATTERIES),
        passive=entry.data.get(CONF_PASSIVE, DEFAULT_PASSIVE),
        timeout_tolerance=entry.data.get(CONF_TIMEOUT_TOLERANCE, DEFAULT_TIMEOUT_TOLERANCE),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()
    return unload_ok
