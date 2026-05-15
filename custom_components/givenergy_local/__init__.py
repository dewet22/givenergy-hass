from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from givenergy_modbus.client import commands
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    DEFAULT_PASSIVE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT_TOLERANCE,
    DOMAIN,
    PLATFORMS,
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_REBOOT_INVERTER,
)
from .coordinator import GivEnergyUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})


def _coordinator_for_device(
    hass: HomeAssistant, device_id: str
) -> GivEnergyUpdateCoordinator | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is not None:
            return coordinator
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GivEnergyUpdateCoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        passive=entry.data.get(CONF_PASSIVE, DEFAULT_PASSIVE),
        timeout_tolerance=entry.data.get(CONF_TIMEOUT_TOLERANCE, DEFAULT_TIMEOUT_TOLERANCE),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_REBOOT_INVERTER):

        async def handle_reboot_inverter(call: ServiceCall) -> None:
            c = _coordinator_for_device(hass, call.data["device_id"])
            if c is None or c._client is None or not c._client.connected:
                raise HomeAssistantError(
                    f"GivEnergy inverter for device {call.data['device_id']!r} "
                    "is not currently connected"
                )
            await c._client.one_shot_command(commands.set_inverter_reboot())

        async def handle_calibrate_battery_soc(call: ServiceCall) -> None:
            c = _coordinator_for_device(hass, call.data["device_id"])
            if c is None or c._client is None or not c._client.connected:
                raise HomeAssistantError(
                    f"GivEnergy inverter for device {call.data['device_id']!r} "
                    "is not currently connected"
                )
            await c._client.one_shot_command(commands.set_calibrate_battery_soc())

        async def handle_generate_dashboard(_call: ServiceCall) -> None:
            from .dashboard import generate_dashboard

            for coordinator in hass.data.get(DOMAIN, {}).values():
                if coordinator.data is None:
                    continue
                inv = coordinator.data.inverter.serial_number.lower()
                bats = [b.serial_number.lower() for b in coordinator.data.batteries]
                yaml = generate_dashboard(inv, bats)
                filename = f"dashboard_givenergy_{inv}.yaml"
                www_dir = Path(hass.config.path("www"))
                www_dir.mkdir(exist_ok=True)
                await hass.async_add_executor_job((www_dir / filename).write_text, yaml)
                url = f"/local/{filename}"
                _LOGGER.info("GivEnergy dashboard available at %s", url)
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "GivEnergy dashboard generated",
                        "message": (
                            f"Dashboard ready — [download YAML]({url})\n\n"
                            "Go to **Settings → Dashboards → Add Dashboard** "
                            "and paste the contents into the raw config editor."
                        ),
                        "notification_id": f"givenergy_dashboard_{inv}",
                    },
                )

        hass.services.async_register(
            DOMAIN, SERVICE_REBOOT_INVERTER, handle_reboot_inverter, SERVICE_DEVICE_SCHEMA
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CALIBRATE_BATTERY_SOC,
            handle_calibrate_battery_soc,
            SERVICE_DEVICE_SCHEMA,
        )
        hass.services.async_register(DOMAIN, SERVICE_GENERATE_DASHBOARD, handle_generate_dashboard)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_REBOOT_INVERTER)
        hass.services.async_remove(DOMAIN, SERVICE_CALIBRATE_BATTERY_SOC)
        hass.services.async_remove(DOMAIN, SERVICE_GENERATE_DASHBOARD)

    return unload_ok
