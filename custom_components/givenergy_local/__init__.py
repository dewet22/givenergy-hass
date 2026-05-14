from __future__ import annotations

from givenergy_modbus.client import commands
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall

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
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_REBOOT_INVERTER,
)
from .coordinator import GivEnergyUpdateCoordinator


def _coordinators(hass: HomeAssistant) -> list[GivEnergyUpdateCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


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

    if not hass.services.has_service(DOMAIN, SERVICE_REBOOT_INVERTER):

        async def handle_reboot_inverter(_call: ServiceCall) -> None:
            for c in _coordinators(hass):
                if c._client and c._client.connected:
                    await c._client.one_shot_command(commands.set_inverter_reboot())

        async def handle_calibrate_battery_soc(_call: ServiceCall) -> None:
            for c in _coordinators(hass):
                if c._client and c._client.connected:
                    await c._client.one_shot_command(commands.set_calibrate_battery_soc())

        hass.services.async_register(DOMAIN, SERVICE_REBOOT_INVERTER, handle_reboot_inverter)
        hass.services.async_register(DOMAIN, SERVICE_CALIBRATE_BATTERY_SOC, handle_calibrate_battery_soc)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_REBOOT_INVERTER)
        hass.services.async_remove(DOMAIN, SERVICE_CALIBRATE_BATTERY_SOC)

    return unload_ok
