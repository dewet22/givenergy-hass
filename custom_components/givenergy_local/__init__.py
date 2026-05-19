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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CONF_PASSIVE,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    DEFAULT_PASSIVE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_REBOOT_INVERTER,
)
from .coordinator import GivEnergyUpdateCoordinator
from .dashboard import DASHBOARD_VERSION

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_STORAGE_KEY = f"{DOMAIN}.dashboard"
_DASHBOARD_STORAGE_VERSION = 1

SERVICE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

SERVICE_GENERATE_DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Optional("max_power_kw", default=10): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)


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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop the user-tunable retry/tolerance knobs from older config entries.

    The library now ships a calibrated retry_delay default, and the previous
    knobs were doing more harm than good in practice (users dialling them up
    to defensive-but-counterproductive values). Strip them so everyone runs
    on the integration's current defaults; storage stays clean rather than
    carrying inert fields that have no effect.
    """
    if entry.version > 2:
        return False
    if entry.version == 1:
        data = {**entry.data}
        data.pop(CONF_TIMEOUT_TOLERANCE, None)
        data.pop(CONF_RETRIES, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GivEnergyUpdateCoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        passive=entry.data.get(CONF_PASSIVE, DEFAULT_PASSIVE),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    store = Store(hass, _DASHBOARD_STORAGE_VERSION, _DASHBOARD_STORAGE_KEY)
    stored = await store.async_load() or {}
    stored_version = stored.get("schema_version", 0)

    # Clean up repair issues from all previous schema versions.
    for v in range(1, DASHBOARD_VERSION):
        ir.async_delete_issue(hass, DOMAIN, f"dashboard_outdated_v{v}")

    if 0 < stored_version < DASHBOARD_VERSION:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"dashboard_outdated_v{DASHBOARD_VERSION}",
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="dashboard_outdated",
            translation_placeholders={
                "old_version": str(stored_version),
                "new_version": str(DASHBOARD_VERSION),
            },
            data={
                "max_power_kw": stored.get("max_power_kw", 10),
                "old_version": stored_version,
                "new_version": DASHBOARD_VERSION,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, f"dashboard_outdated_v{DASHBOARD_VERSION}")

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

        async def handle_generate_dashboard(call: ServiceCall) -> None:
            from .dashboard import generate_dashboard

            max_power_kw = call.data["max_power_kw"]
            for coordinator in hass.data.get(DOMAIN, {}).values():
                if coordinator.data is None:
                    continue
                inv = coordinator.data.inverter.serial_number.lower()
                bats = [b.serial_number.lower() for b in coordinator.data.batteries]
                yaml = generate_dashboard(inv, bats, max_power_kw=max_power_kw)
                filename = f"dashboard_givenergy_{inv}.yaml"
                www_dir = Path(hass.config.path("www"))
                await hass.async_add_executor_job(lambda d=www_dir: d.mkdir(exist_ok=True))
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
            await store.async_save(
                {"schema_version": DASHBOARD_VERSION, "max_power_kw": max_power_kw}
            )
            ir.async_delete_issue(hass, DOMAIN, f"dashboard_outdated_v{DASHBOARD_VERSION}")

        hass.services.async_register(
            DOMAIN, SERVICE_REBOOT_INVERTER, handle_reboot_inverter, SERVICE_DEVICE_SCHEMA
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CALIBRATE_BATTERY_SOC,
            handle_calibrate_battery_soc,
            SERVICE_DEVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_DASHBOARD,
            handle_generate_dashboard,
            SERVICE_GENERATE_DASHBOARD_SCHEMA,
        )

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
