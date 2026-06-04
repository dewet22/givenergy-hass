from __future__ import annotations

import importlib.metadata
import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

import voluptuous as vol
from givenergy_modbus.client import commands
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.persistent_notification import (
    async_create as async_create_notification,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PASSIVE,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    DEFAULT_PASSIVE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXPOSE_RECOMMENDED_ENTITY_KEYS,
    PLATFORMS,
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_CAPTURE_FRAMES,
    SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_REBOOT_INVERTER,
    SERVICE_REDETECT_PLANT,
    SERVICE_SET_SYSTEM_DATETIME,
)
from .coordinator import GivEnergyUpdateCoordinator
from .dashboard import DASHBOARD_VERSION
from .http import (
    CaptureDownloadView,
    CaptureLandingView,
    build_capture_notification_url,
    capture_dir,
)

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_STORAGE_KEY = f"{DOMAIN}.dashboard"
_DASHBOARD_STORAGE_VERSION = 1

# Bundled cell-balance heatmap card, served from this integration's package and
# auto-loaded on the frontend so the generated dashboard's custom:ge-cell-heatmap
# resolves without a manual HACS/resource install. Bump _CARD_VERSION whenever
# the JS changes, to bust the browser cache.
_CARD_FILENAME = "ge-cell-heatmap.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILENAME}"
_CARD_VERSION = "2"

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


# Callers may identify the target inverter by HA-assigned device_id (convenient
# in the Settings → Services UI) OR by inverter serial (convenient in dashboards
# and automations that only know the serial). Exactly one must be supplied.
def _require_one_of_device_or_serial(value: dict) -> dict:
    if "device_id" not in value and "serial" not in value:
        raise vol.Invalid("Supply either 'device_id' or 'serial'")
    return value


SERVICE_DEVICE_OR_SERIAL_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Exclusive("device_id", "target"): cv.string,
            vol.Exclusive("serial", "target"): cv.string,
        },
        _require_one_of_device_or_serial,
    )
)

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

# Default targets `conversation`, which covers Assist, the LLM tools API, and
# MCP-via-conversation. Users wanting Alexa/Google can override; they may also
# add unknown values (e.g. a custom assistant) — async_expose_entity accepts
# any string, so we don't validate against a known set here.
SERVICE_EXPOSE_RECOMMENDED_ENTITIES_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("assistants", default=["conversation"]): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.string]
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


def _entry_id_for_serial(hass: HomeAssistant, serial: str) -> str | None:
    """Return the config-entry ID whose plant matches `serial`, or None.

    The config flow stores the inverter serial as the entry's unique_id (normalised
    to uppercase). The device registry also carries an identifier
    ("givenergy_local", serial) — we try both to be robust across installs where
    one or the other might be absent.
    """
    serial_upper = serial.upper()
    # Primary: config-entry unique_id (set by the config flow to the serial).
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id and entry.unique_id.upper() == serial_upper:
            return entry.entry_id
    # Fallback: device registry identifier.
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, serial_upper)})
    if device is not None:
        for entry_id in device.config_entries:
            if entry_id in hass.data.get(DOMAIN, {}):
                return entry_id
    return None


def _resolve_target(hass: HomeAssistant, call_data: dict) -> tuple[str | None, str]:
    """Resolve a device_id-or-serial service call to a config entry_id.

    Returns (entry_id, error_message). If resolution succeeds, error_message is
    empty; if it fails, entry_id is None and error_message explains why.
    """
    if "serial" in call_data:
        entry_id = _entry_id_for_serial(hass, call_data["serial"])
        if entry_id is None:
            return None, f"No GivEnergy inverter found for serial {call_data['serial']!r}"
        return entry_id, ""
    # device_id path — find the config entry via the device registry.
    device_id = call_data["device_id"]
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None, f"No GivEnergy device found for device_id {device_id!r}"
    entry_id = next(
        (eid for eid in device.config_entries if eid in hass.data.get(DOMAIN, {})),
        None,
    )
    if entry_id is None:
        return None, f"No GivEnergy config entry found for device {device_id!r}"
    return entry_id, ""


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


async def _async_register_frontend_card(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled cell-heatmap card.

    The card module ships inside this integration's ``www/`` dir; we expose it
    at a stable URL and register it as an extra frontend module so the generated
    dashboard's ``custom:ge-cell-heatmap`` resolves on any dashboard without a
    manual HACS/resource install.

    Called once from :func:`async_setup` (component scope), so the static-path
    registration happens a single time for the integration regardless of how
    many inverters/EMS config entries exist — no per-entry race on the shared URL.
    """
    if hass.http is None:
        # http isn't initialised (e.g. the test harness has no web server). In
        # production it's a bootstrap dependency and always present, so this only
        # skips where there is nothing to serve from anyway.
        return
    try:
        card_path = Path(__file__).parent / "www" / _CARD_FILENAME
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_URL, str(card_path), False)]
        )
        add_extra_js_url(hass, f"{_CARD_URL}?v={_CARD_VERSION}")
    except Exception as exc:  # noqa: BLE001
        # The bundled card is cosmetic (a dashboard heatmap). Registering it once
        # at component scope means a failure here is genuinely unexpected, but it
        # must still never take down the integration — log and carry on.
        _LOGGER.warning("Could not register the bundled cell-heatmap card: %s", exc)


async def _build_capture_header(
    hass: HomeAssistant, *, generated: datetime, duration: float, frame_count: int
) -> str:
    """Hash-prefixed environment header prepended to a wire capture (issue #64).

    Pure environment introspection — deliberately no ``coordinator.data`` access
    (works even if the coordinator is in a bad state) and no inverter
    serial/model/firmware, which the library's redaction principle keeps out of
    shared diagnostics (those are recoverable from the wire frames by anyone with
    a parser anyway).
    """
    try:
        library_version = importlib.metadata.version("givenergy-modbus")
    except importlib.metadata.PackageNotFoundError:
        library_version = "unknown"
    integration = await async_get_integration(hass, DOMAIN)
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    lines = [
        "# GivEnergy Local — Modbus wire capture",
        f"# Generated:      {generated.isoformat()}",
        f"# Duration:       {duration:g}s",
        f"# Frames:         {frame_count}",
        "#",
        f"# Home Assistant: {HA_VERSION}",
        f"# Python:         {python_version}",
        f"# OS:             {platform.platform()}",
        f"# Integration:    {integration.version}",
        f"# Library:        givenergy-modbus {library_version}",
        "#",
    ]
    return "\n".join(lines) + "\n"


async def _async_register_capture_http(hass: HomeAssistant) -> None:
    """Register the capture landing/download views and ensure the capture dir.

    Component-scope (run once from :func:`async_setup`): the views are global and
    the directory is shared across config entries, so neither belongs per-entry.
    """
    await hass.async_add_executor_job(lambda: capture_dir(hass).mkdir(exist_ok=True))
    if hass.http is None:
        # No web server (e.g. a minimal test harness) — nothing to serve from.
        return
    hass.http.register_view(CaptureLandingView(hass))
    hass.http.register_view(CaptureDownloadView(hass))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Integration-wide setup, run once before any config entry.

    The bundled frontend card is an integration-level singleton (one JS module,
    one custom element), so it's registered here rather than per config entry —
    which previously raced on the shared static path when several entries set up
    concurrently.
    """
    await _async_register_frontend_card(hass)
    await _async_register_capture_http(hass)
    return True


# External HACS cards the generated dashboard depends on (the bundled
# ge-cell-heatmap is served by us and needs no check). Keep in sync with the
# custom: cards emitted in dashboard.py.
_REQUIRED_HACS_CARDS = ("apexcharts-card", "power-flow-card-plus")


async def _missing_dashboard_cards(hass: HomeAssistant) -> list[str]:
    """Best-effort list of required HACS cards with no registered Lovelace resource.

    Returns [] when all are present *or* when the resource registry can't be
    read — we warn only on a confident miss, never cry wolf. Only storage-mode
    resources are enumerable; YAML-mode users register resources in
    configuration.yaml and won't appear here, so the warning is advisory.
    """
    try:
        resources = getattr(hass.data.get("lovelace"), "resources", None)
        if resources is None:
            return []
        items = resources.async_items()
        if not items and hasattr(resources, "async_load"):
            await resources.async_load()
            items = resources.async_items()
        urls = " ".join(str(item.get("url", "")) for item in items)
    except Exception as exc:  # noqa: BLE001 - advisory check must never break generation
        _LOGGER.debug("Could not read Lovelace resources for pre-flight check: %s", exc)
        return []
    return [card for card in _REQUIRED_HACS_CARDS if card not in urls]


# unique_id suffixes renamed in givenergy-modbus #174 (2.1.1). The old data is
# valid — IR35 was always AC charge, merely mislabelled "load" — so re-point the
# existing registry entry to the new unique_id, carrying its history, statistics
# and customisations across rather than orphaning it and starting fresh.
_RENAMED_UNIQUE_ID_SUFFIXES = {
    # givenergy-modbus #174 (2.1.1): IR35 was AC charge, not house load.
    "e_load_day": "e_ac_charge_today",
    # givenergy-modbus #174/#176 (2.1.2): IR44/IR45-46 are PV generation, not
    # inverter AC output. Move both sensors together so today+total stay paired.
    "e_inverter_out_day": "e_pv_generation_today",
    "e_inverter_out_total": "e_pv_generation_total",
    # #52: p_grid_out (IR30) is a signed net flow, not export-only — rename the
    # surfaced entity to "Grid Power" to match. Existing history is valid (the
    # underlying register hasn't changed), so re-point in place.
    "p_grid_out": "grid_power",
}


def _migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-point entities registered under a renamed unique_id suffix in place."""
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        for old, new in _RENAMED_UNIQUE_ID_SUFFIXES.items():
            if not ent.unique_id.endswith(f"_{old}"):
                continue
            new_uid = ent.unique_id[: -len(old)] + new
            if registry.async_get_entity_id(ent.domain, DOMAIN, new_uid):
                # Target already exists (already migrated, or a genuine collision)
                # — don't clobber it; leave the old entry for manual cleanup.
                _LOGGER.debug(
                    "Skipping unique_id migration for %s: %s already exists",
                    ent.entity_id,
                    new_uid,
                )
                break
            _LOGGER.info("Migrating unique_id %s -> %s", ent.unique_id, new_uid)
            registry.async_update_entity(ent.entity_id, new_unique_id=new_uid)
            break


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
    #
    # Only persist a CLEAN seed: if the seed poll was partial (last_partial_failures
    # non-empty), the integration still loads (coordinator serves the partial), but
    # we don't commit a possibly-degraded topology to disk — flaky kit could
    # otherwise vanish permanently on the next warm start. A permanently-partial
    # plant re-detects fresh each cold start and self-heals to a clean persist once
    # the read succeeds.
    if (
        prior_capabilities is None
        and coordinator.data.capabilities is not None
        and not coordinator.last_partial_failures
    ):
        await _save_capabilities(hass, entry.entry_id, coordinator.data.capabilities)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Re-point any entities under renamed unique_ids before the platforms create
    # them, so the existing entity (and its history) is reused rather than orphaned.
    _migrate_unique_ids(hass, entry)

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

    # EMS entity-id realignment prompt. An EMS controller's entities are now named
    # `givenergy_ems_…` (sensor._device_kind); existing installs still carry the old
    # `givenergy_inverter_…` ids until the user runs HA's "Recreate entity IDs" on the
    # device. Surface a repair issue while any stale ids remain — it self-clears once
    # recreated. (We deliberately don't auto-migrate; see the dashboard module note.)
    if coordinator.data.ems is not None:
        reg = er.async_get(hass)
        inv_serial = coordinator.data.inverter_serial_number.lower()
        issue_id = f"ems_entity_ids_outdated_{entry.entry_id}"
        has_stale = any(
            e.entity_id.startswith(f"{e.domain}.givenergy_inverter_{inv_serial}_")
            for e in er.async_entries_for_config_entry(reg, entry.entry_id)
        )
        if has_stale:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="ems_entity_ids_outdated",
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

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

        async def handle_set_system_datetime(call: ServiceCall) -> None:
            entry_id, err = _resolve_target(hass, call.data)
            if err:
                raise HomeAssistantError(err)
            c = hass.data.get(DOMAIN, {}).get(entry_id)
            if c is None or c._client is None or not c._client.connected:
                raise HomeAssistantError("GivEnergy inverter is not currently connected")
            # Sync the inverter's clock to Home Assistant's current local time.
            await c._client.one_shot_command(commands.set_system_date_time(dt_util.now()))

        async def handle_generate_dashboard(call: ServiceCall) -> None:
            from .dashboard import generate_dashboard

            max_power_kw = call.data["max_power_kw"]
            missing = await _missing_dashboard_cards(hass)
            warning = ""
            if missing:
                warning = (
                    "\n\n**Note:** these cards the dashboard needs don't appear to be "
                    "installed — affected cards will show \"Custom element doesn't "
                    'exist" until you add them via **HACS → Frontend**:\n'
                    + "\n".join(f"- `{card}`" for card in missing)
                    + "\n\n(If you register Lovelace resources via YAML, ignore this.)"
                )
            for coordinator in hass.data.get(DOMAIN, {}).values():
                if coordinator.data is None:
                    continue
                inv = coordinator.data.inverter.serial_number.lower()
                bats = [b.serial_number.lower() for b in coordinator.data.batteries]
                is_ems = coordinator.data.ems is not None
                caps = coordinator.data.capabilities
                has_ac_config_block = bool(
                    caps and caps.has_ac_config_block and not caps.is_three_phase
                )
                # TODO: source from `caps.has_smart_load` once givenergy-modbus
                # exposes the capability (#181, targeted at 2.1.3). Until then
                # always emit; rows render as unavailable on non-Smart-Load installs.
                has_smart_load = not is_ems
                yaml = generate_dashboard(
                    inv,
                    bats,
                    max_power_kw=max_power_kw,
                    is_ems=is_ems,
                    has_ac_config_block=has_ac_config_block,
                    has_smart_load=has_smart_load,
                )
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
                            "and paste the contents into the raw config editor." + warning
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

                header = await _build_capture_header(
                    hass,
                    generated=dt_util.now(),
                    duration=float(duration),
                    frame_count=len(frames),
                )
                body = "\n".join(frames) if frames else "(no frames captured)"
                content = header + "\n" + body + "\n"

                epoch = int(dt_util.utcnow().timestamp())
                filename = f"capture_givenergy_{epoch}.txt"
                directory = capture_dir(hass)
                await hass.async_add_executor_job(lambda d=directory: d.mkdir(exist_ok=True))
                await hass.async_add_executor_job((directory / filename).write_text, content)

                landing_url = build_capture_notification_url(hass, filename)
                _LOGGER.info(
                    "GivEnergy frame capture saved to %s (%d frames)", filename, len(frames)
                )
                async_create_notification(
                    hass,
                    (
                        f"Captured {len(frames)} frames over {duration} s.\n\n"
                        f"[Open the capture]({landing_url}) to inspect it, download "
                        "the file, or open a pre-filled GitHub issue."
                    ),
                    title="GivEnergy frame capture complete",
                    notification_id=f"givenergy_capture_{inv}",
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
            SERVICE_SET_SYSTEM_DATETIME,
            handle_set_system_datetime,
            SERVICE_DEVICE_OR_SERIAL_SCHEMA,
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
            target_entry_id, err = _resolve_target(hass, call.data)
            if err or target_entry_id is None:
                raise HomeAssistantError(err or "Could not resolve target entry")
            await _capabilities_store(hass, target_entry_id).async_remove()
            hass.config_entries.async_schedule_reload(target_entry_id)

        async def handle_expose_recommended_entities(call: ServiceCall) -> None:
            # Mirrors the dashboard generator's UX: a one-shot service that
            # seeds an opinionated starting set. Idempotent — re-running just
            # re-confirms exposure for whatever subset still exists. Doesn't
            # un-expose anything, so a user who manually removes one of these
            # entities from Assist won't have their choice fought on next call.
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
            assistants = call.data["assistants"]

            # Match entries by unique_id suffix — sensor.py builds these as
            # `f"{serial}_{description.key}"`, so endswith(_key) is unambiguous
            # for the keys in EXPOSE_RECOMMENDED_ENTITY_KEYS (none of them are
            # suffixes of another key).
            entity_reg = er.async_get(hass)
            matched: list[tuple[str, str]] = []  # (entity_id, display_name)
            for entry in er.async_entries_for_config_entry(entity_reg, target_entry_id):
                # Disabled entities can't usefully be exposed — they have no
                # state in the registry for the assistant to consume.
                if entry.disabled_by is not None:
                    continue
                for key in EXPOSE_RECOMMENDED_ENTITY_KEYS:
                    if entry.unique_id.endswith(f"_{key}"):
                        for assistant in assistants:
                            async_expose_entity(hass, assistant, entry.entity_id, True)
                        matched.append((entry.entity_id, entry.name or entry.original_name or key))
                        break

            if not matched:
                raise HomeAssistantError(
                    f"No headline entities found for device {call.data['device_id']!r} — "
                    "the integration may still be initialising"
                )

            names = "\n".join(f"- {name} (`{entity_id}`)" for entity_id, name in matched)
            assistant_list = ", ".join(assistants)
            async_create_notification(
                hass,
                (
                    f"Exposed {len(matched)} entities to {assistant_list} for "
                    f"`{device.name_by_user or device.name}`:\n\n{names}\n\n"
                    "Review or un-expose any of these in "
                    "**Settings → Voice assistants → Expose**."
                ),
                title="GivEnergy: headline entities exposed",
                notification_id=f"givenergy_exposed_{target_entry_id}",
            )

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
            SERVICE_DEVICE_OR_SERIAL_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
            handle_expose_recommended_entities,
            SERVICE_EXPOSE_RECOMMENDED_ENTITIES_SCHEMA,
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
        hass.services.async_remove(DOMAIN, SERVICE_SET_SYSTEM_DATETIME)
        hass.services.async_remove(DOMAIN, SERVICE_GENERATE_DASHBOARD)
        hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_FRAMES)
        hass.services.async_remove(DOMAIN, SERVICE_REDETECT_PLANT)
        hass.services.async_remove(DOMAIN, SERVICE_EXPOSE_RECOMMENDED_ENTITIES)

    return unload_ok
