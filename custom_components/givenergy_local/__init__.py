from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from givenergy_modbus.client import commands
from givenergy_modbus.model.plant import PlantCapabilities
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
    SERVICE_CAPTURE_FRAMES,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_REBOOT_INVERTER,
    SERVICE_REDETECT_PLANT,
)
from .coordinator import GivEnergyUpdateCoordinator
from .dashboard import DASHBOARD_VERSION

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_STORAGE_KEY = f"{DOMAIN}.dashboard"
_DASHBOARD_STORAGE_VERSION = 1

# Per-config-entry topology cache. PlantCapabilities is persisted as
# `to_dict()` directly (no envelope) following HA Core's Store convention —
# future shape changes go through `Store._async_migrate_func` on a subclass,
# not an in-payload version field. Library-internal schema evolution is
# already handled by `PlantCapabilities.from_dict()`.
_CAPABILITIES_STORAGE_KEY_PREFIX = f"{DOMAIN}.plant_capabilities"
_CAPABILITIES_STORAGE_VERSION = 1


def _capabilities_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(
        hass,
        _CAPABILITIES_STORAGE_VERSION,
        f"{_CAPABILITIES_STORAGE_KEY_PREFIX}.{entry_id}",
    )


async def _load_capabilities(hass: HomeAssistant, entry_id: str) -> PlantCapabilities | None:
    """Load the persisted topology for `entry_id`, or None on miss/corrupt.

    `Store` already absorbs file-not-found and JSON-decode errors and returns
    None; we only add a guard around `from_dict()` for cases the library
    rejects (hand-edited files, payloads from a future library schema, etc.).
    Callers treat None as a cue to run a cold `detect()`.
    """
    payload = await _capabilities_store(hass, entry_id).async_load()
    if payload is None:
        return None
    try:
        return PlantCapabilities.from_dict(payload)
    except (KeyError, ValueError, TypeError) as exc:
        _LOGGER.debug("Capabilities cache rejected by library from_dict(): %s", exc)
        return None


async def _save_capabilities(
    hass: HomeAssistant, entry_id: str, capabilities: PlantCapabilities
) -> None:
    await _capabilities_store(hass, entry_id).async_save(capabilities.to_dict())


SERVICE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

SERVICE_GENERATE_DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Optional("max_power_kw", default=10): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)

SERVICE_CAPTURE_FRAMES_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("duration", default=60): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
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
    # Persisted topology lets the coordinator skip the cold-detect sweep on
    # most reconnects/restarts. Client.detect(prior=...) accepts the cached
    # topology as a hint and only re-probes slots the prior asserts non-empty;
    # on real topology change it raises PlantTopologyMismatch.
    prior_capabilities = await _load_capabilities(hass, entry.entry_id)

    async def _on_topology_changed(actual: PlantCapabilities) -> None:
        # async_schedule_reload is the documented preferred path from inside
        # integration code: it cancels any pending setup retry before queuing
        # the reload task, avoiding a race where the retry fires mid-reload.
        await _save_capabilities(hass, entry.entry_id, actual)
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"plant_topology_changed_{entry.entry_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="plant_topology_changed",
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    coordinator = GivEnergyUpdateCoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        passive=entry.data.get(CONF_PASSIVE, DEFAULT_PASSIVE),
        prior_capabilities=prior_capabilities,
        on_topology_changed=_on_topology_changed,
    )

    await coordinator.async_config_entry_first_refresh()

    # Seed the cache on cold start (no prior loaded). Warm-hit doesn't need a
    # write — the prior we just loaded is what's on the wire. Mismatch is
    # already covered by _on_topology_changed having saved exc.actual.
    if (
        prior_capabilities is None
        and coordinator.data is not None
        and coordinator.data.capabilities is not None
    ):
        await _save_capabilities(hass, entry.entry_id, coordinator.data.capabilities)

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

        async def handle_capture_frames(call: ServiceCall) -> None:
            device_id = call.data.get("device_id")
            duration = call.data["duration"]

            if device_id is not None:
                c = _coordinator_for_device(hass, device_id)
                if c is None or c._client is None or not c._client.connected:
                    raise HomeAssistantError(
                        f"GivEnergy inverter for device {device_id!r} is not currently connected"
                    )
                coordinators = [c]
            else:
                coordinators = [
                    c
                    for c in hass.data.get(DOMAIN, {}).values()
                    if c._client is not None and c._client.connected
                ]
                if not coordinators:
                    raise HomeAssistantError("No connected GivEnergy inverter found")

            for coordinator in coordinators:
                if coordinator.data is None or coordinator._client is None:
                    continue
                inv = coordinator.data.inverter.serial_number.lower()
                frames: list[str] = []

                def _sink(direction: str, data: bytes, bucket: list[str] = frames) -> None:
                    bucket.append(f"{direction}: {data.hex()}")

                await coordinator._client.capture_frames(_sink, duration=float(duration))

                filename = f"capture_givenergy_{inv}.txt"
                www_dir = Path(hass.config.path("www"))
                await hass.async_add_executor_job(lambda d=www_dir: d.mkdir(exist_ok=True))
                content = "\n".join(frames) if frames else "(no frames captured)"
                await hass.async_add_executor_job((www_dir / filename).write_text, content)
                url = f"/local/{filename}"
                _LOGGER.info("GivEnergy frame capture saved at %s (%d frames)", url, len(frames))
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "GivEnergy frame capture complete",
                        "message": (
                            f"Captured {len(frames)} frames over {duration} s — "
                            f"[download]({url})\n\n"
                            "Attach this file when reporting connectivity issues."
                        ),
                        "notification_id": f"givenergy_capture_{inv}",
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
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_DASHBOARD,
            handle_generate_dashboard,
            SERVICE_GENERATE_DASHBOARD_SCHEMA,
        )

        async def handle_redetect_plant(call: ServiceCall) -> None:
            # Clear the cached topology for this device's entry and trigger a
            # reload — the next setup_entry will see no prior and do a cold
            # detect(), which is exactly the "I changed the hardware, please
            # rediscover" semantic the issue calls for.
            device = dr.async_get(hass).async_get(call.data["device_id"])
            if device is None:
                raise HomeAssistantError(
                    f"No GivEnergy device found for {call.data['device_id']!r}"
                )
            target_entry_id = next(
                (eid for eid in device.config_entries if eid in hass.data.get(DOMAIN, {})),
                None,
            )
            if target_entry_id is None:
                raise HomeAssistantError(
                    f"No GivEnergy config entry found for device {call.data['device_id']!r}"
                )
            await _capabilities_store(hass, target_entry_id).async_remove()
            hass.config_entries.async_schedule_reload(target_entry_id)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CAPTURE_FRAMES,
            handle_capture_frames,
            SERVICE_CAPTURE_FRAMES_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REDETECT_PLANT,
            handle_redetect_plant,
            SERVICE_DEVICE_SCHEMA,
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
        hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_FRAMES)
        hass.services.async_remove(DOMAIN, SERVICE_REDETECT_PLANT)

    return unload_ok
