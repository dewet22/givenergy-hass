from __future__ import annotations

import importlib.metadata
import logging
import platform
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TypedDict

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
from homeassistant.core import HomeAssistant, ServiceCall, callback
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
    CONF_BATTERY_DATA_ONLY,
    CONF_PASSIVE,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    CONF_WARN_CLOCK_DRIFT,
    DEFAULT_BATTERY_DATA_ONLY,
    DEFAULT_PASSIVE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WARN_CLOCK_DRIFT,
    DOMAIN,
    EXPOSE_RECOMMENDED_ENTITY_KEYS,
    PLATFORMS,
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_CAPTURE_FRAMES,
    SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
    SERVICE_REBOOT_INVERTER,
    SERVICE_REDETECT_PLANT,
    SERVICE_SET_SYSTEM_DATETIME,
    SYSTEM_TIME_DRIFT_THRESHOLD,
    resolve_experimental_client_kwargs,
    system_time_drift,
)
from .coordinator import GivEnergyUpdateCoordinator, missing_devices
from .http import (
    CaptureDownloadView,
    CaptureLandingView,
    build_capture_notification_url,
    capture_dir,
    write_capture,
)

_LOGGER = logging.getLogger(__name__)

# Bundled frontend module: the dashboard strategy (custom:givenergy) and the
# cell-balance heatmap card (custom:ge-cell-heatmap) are shipped together in a
# single JS file, served from this integration's package and auto-loaded so both
# resolve on any install without a manual HACS/resource registration. Bump
# _STRATEGY_VERSION whenever the JS changes, to bust the browser cache.
_STRATEGY_FILENAME = "ge-strategy.js"
_STRATEGY_URL = f"/{DOMAIN}/{_STRATEGY_FILENAME}"
# Glyph-subsetted woff2 fonts (Fraunces + Geist Mono) used by the flow card,
# served from the same package dir so they resolve offline without a CDN.
_FONTS_DIRNAME = "fonts"
_FONTS_URL = f"/{DOMAIN}/{_FONTS_DIRNAME}"
_STRATEGY_VERSION = "11"

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


async def _redetect_plant_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Discard an entry's cached topology and reload it — a cold re-detect.

    Shared by the `redetect_plant` service and the Re-detect Plant button: with no
    prior on disk the next setup_entry runs a full `detect()` sweep, which is the
    "I changed the hardware (added/recovered a battery), please rediscover" path.
    """
    await _capabilities_store(hass, entry_id).async_remove()
    hass.config_entries.async_schedule_reload(entry_id)


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
    """Serve and auto-load the bundled frontend module (strategy + heatmap card).

    The single JS file ships inside this integration's ``www/`` dir; we expose
    it at a stable URL and register it as an extra frontend module so both
    ``custom:givenergy`` (the dashboard strategy) and ``custom:ge-cell-heatmap``
    resolve on any install without a manual HACS/resource registration.

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
        www_dir = Path(__file__).parent / "www"
        module_path = www_dir / _STRATEGY_FILENAME
        fonts_path = www_dir / _FONTS_DIRNAME
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(_STRATEGY_URL, str(module_path), False),
                StaticPathConfig(_FONTS_URL, str(fonts_path), True),
            ]
        )
        add_extra_js_url(hass, f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}")
    except Exception as exc:  # noqa: BLE001
        # The bundled module is cosmetic (dashboard frontend). Registering it
        # once at component scope means a failure here is genuinely unexpected,
        # but it must still never take down the integration — log and carry on.
        _LOGGER.warning("Could not register the bundled frontend module: %s", exc)


def _givenergy_modbus_version() -> str:
    """Installed givenergy-modbus version, "unknown" if unresolvable.

    Blocking (reads dist-info from disk) — call via the executor.
    """
    try:
        return importlib.metadata.version("givenergy-modbus")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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
    # Package-metadata lookup reads dist-info off disk — executor, not loop
    # (HA's blocking-call detector flags it otherwise).
    library_version = await hass.async_add_executor_job(_givenergy_modbus_version)
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
    await hass.async_add_executor_job(lambda: capture_dir(hass).mkdir(mode=0o700, exist_ok=True))
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


# unique_id suffixes renamed in givenergy-modbus #174 (2.1.1). The old data is
# valid — IR35 was always AC charge, merely mislabelled "load" — so re-point the
# existing registry entry to the new unique_id, carrying its history, statistics
# and customisations across rather than orphaning it and starting fresh.

# Values: (new_uid_suffix, old_entity_id_slug | None).
# old_entity_id_slug is the name-slug the entity carried before renaming; None
# means no entity_id rename is needed (unique_id suffix change only).
_RENAMED_UNIQUE_ID_SUFFIXES: dict[str, tuple[str, str | None]] = {
    # givenergy-modbus #174 (2.1.1): IR35 was AC charge, not house load.
    "e_load_day": ("e_ac_charge_today", None),
    # givenergy-modbus #174/#176 (2.1.2): IR44/IR45-46 are PV generation, not
    # inverter AC output. Move both sensors together so today+total stay paired.
    "e_inverter_out_day": ("e_pv_generation_today", None),
    "e_inverter_out_total": ("e_pv_generation_total", None),
    # #52: p_grid_out (IR30) is a signed net flow, not export-only — rename the
    # surfaced entity to "Grid Power" to match. Existing history is valid (the
    # underlying register hasn't changed), so re-point in place.
    # entity_id was "…_grid_export_power"; must also be renamed so dashboard
    # references to "…_grid_power" resolve correctly.
    "p_grid_out": ("grid_power", "grid_export_power"),
}


class _EntityUpdates(TypedDict, total=False):
    """The kwargs subset _migrate_unique_ids passes to async_update_entity."""

    new_unique_id: str
    new_entity_id: str


def _migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-point entities registered under a renamed unique_id suffix in place.

    Both halves are independent and idempotent: the unique_id rename fires only
    while the old suffix is still present, and the entity_id rename fires whenever
    the old name-slug is still present — including on installs where an earlier
    release already migrated the unique_id but not the entity_id (the entity_id
    rename was added later). Keying the entity_id rename on the unique_id would
    miss exactly those installs.
    """
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        for old, (new, old_slug) in _RENAMED_UNIQUE_ID_SUFFIXES.items():
            uid_stale = ent.unique_id.endswith(f"_{old}")
            uid_already_new = ent.unique_id.endswith(f"_{new}")
            entity_id_stale = bool(old_slug) and ent.entity_id.endswith(f"_{old_slug}")
            if not uid_stale and not (uid_already_new and entity_id_stale):
                continue

            updates: _EntityUpdates = {}
            if uid_stale:
                new_uid = ent.unique_id[: -len(old)] + new
                if registry.async_get_entity_id(ent.domain, DOMAIN, new_uid):
                    # Target unique_id already exists (genuine collision) — don't
                    # clobber it; leave the old entry for manual cleanup.
                    _LOGGER.debug(
                        "Skipping unique_id migration for %s: %s already exists",
                        ent.entity_id,
                        new_uid,
                    )
                else:
                    updates["new_unique_id"] = new_uid
            if entity_id_stale:
                assert old_slug is not None  # entity_id_stale ⇒ old_slug truthy
                new_entity_id = ent.entity_id[: -len(old_slug)] + new
                if registry.async_get(new_entity_id) is not None:
                    _LOGGER.debug(
                        "Skipping entity_id rename for %s: %s already exists",
                        ent.entity_id,
                        new_entity_id,
                    )
                else:
                    updates["new_entity_id"] = new_entity_id
            if updates:
                _LOGGER.info("Migrating %s in place: %s", ent.entity_id, updates)
                registry.async_update_entity(ent.entity_id, **updates)
            break


def _retired_inverter_unique_ids(serial: str) -> set[str]:
    """Unique IDs of the entities retired on an EMS plant (#201).

    On an EMS controller the inverter-level *controls* are suppressed (the EMS
    slots are authoritative) and a few inverter sensors are gated via
    `skip_if_ems` (the controller-local load figures plus Battery Charge/Discharge
    Today, which the 0x11 controller doesn't populate), plus the dropped duplicate
    `ems_status` aggregate. The rest of the inverter sensors stay — the 0x11 block
    carries real plant data (PV/grid/battery/AC) — so they are deliberately NOT in
    this set.
    Keyed by the controller serial; coordinator, battery, AIO and managed-inverter
    entities use different keys or their own serials, so they're excluded.
    """
    # Local imports: these platform modules are imported by the platform setup
    # anyway, and importing them at module scope here risks a load-order cycle.
    from .number import AC_COUPLED_NUMBER_DESCRIPTIONS, NUMBER_DESCRIPTIONS
    from .select import AC_COUPLED_SELECT_DESCRIPTIONS, SELECT_DESCRIPTIONS
    from .sensor import INVERTER_SENSORS
    from .switch import AC_COUPLED_SWITCH_DESCRIPTIONS, SWITCH_DESCRIPTIONS
    from .time import SMART_LOAD_TIME_DESCRIPTIONS, TIME_DESCRIPTIONS

    # All inverter-level controls are suppressed on EMS.
    controls = (
        *SWITCH_DESCRIPTIONS,
        *AC_COUPLED_SWITCH_DESCRIPTIONS,
        *NUMBER_DESCRIPTIONS,
        *AC_COUPLED_NUMBER_DESCRIPTIONS,
        *SELECT_DESCRIPTIONS,
        *AC_COUPLED_SELECT_DESCRIPTIONS,
        *TIME_DESCRIPTIONS,
        *SMART_LOAD_TIME_DESCRIPTIONS,
    )
    keys = {d.key for d in controls}
    # Inverter sensors gated on EMS (controller-local load + Battery Charge/Discharge Today).
    keys.update(d.key for d in INVERTER_SENSORS if d.skip_if_ems)
    # Duplicate EMS aggregate dropped in favour of the retained inverter Status sensor.
    keys.add("ems_status")
    return {f"{serial}_{key}" for key in keys}


def _reconcile_ems_entities(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """Remove inverter-level entities retired on an EMS plant (#201).

    Suppressing creation only affects fresh installs: on an upgraded EMS entry HA
    keeps the existing registry rows when a platform stops adding those entities,
    so the controller would otherwise keep orphaned rows for the entities no longer
    created on EMS (inverter controls, the EMS-gated inverter sensors, the dropped
    ems_status). Remove exactly those (matched by the controller serial + a retired
    key), leaving the retained inverter sensors and all coordinator, battery, AIO,
    managed-inverter and EMS-specific entities untouched.
    """
    registry = er.async_get(hass)
    retired = _retired_inverter_unique_ids(serial)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id in retired:
            _LOGGER.info("Removing inverter entity %s retired on EMS plant (#201)", ent.entity_id)
            registry.async_remove(ent.entity_id)


def _remove_stale_control(registry: er.EntityRegistry, serial: str, domain: str, key: str) -> None:
    """Remove a readability-gated control's stale registry row, if present (#207)."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{serial}_{key}")
    if entity_id is not None:
        _LOGGER.info(
            "Removing control %s: register absent on this device/firmware (#207)", entity_id
        )
        registry.async_remove(entity_id)


def _reconcile_readability_gated_controls(
    hass: HomeAssistant, coordinator: GivEnergyUpdateCoordinator
) -> None:
    """Remove control rows whose register is absent on this device/firmware (#207).

    The control platforms skip creating a skip_if_none control when its register
    reads None, but on an upgraded entry HA keeps the pre-existing row — an
    orphaned, unavailable control the readability gate is meant to remove. Mirror
    the EMS reconciliation: remove exactly those rows pre-platform, reusing each
    platform's own gate so the readability logic isn't duplicated here.
    """
    # Local imports: the platforms import from this package, so importing them at
    # module scope risks a load-order cycle. The gate helpers are the single source
    # of truth for "is this control's register present".
    from .datetime import SYSTEM_TIME_DESCRIPTION
    from .number import AC_COUPLED_NUMBER_DESCRIPTIONS, _include_number
    from .select import SELECT_DESCRIPTIONS, _include_select
    from .time import TIME_DESCRIPTIONS, _include_time

    # A partial seed poll serves last-good with last_partial_failures set, so a None
    # read may be a transient bank failure rather than structural absence. Removing
    # rows now would lose history/customisation and the controls until a reload —
    # reconcile only on a clean seed (#208 review).
    if coordinator.last_partial_failures:
        return

    inverter = coordinator.data.inverter
    serial = coordinator.data.inverter_serial_number
    registry = er.async_get(hass)
    for number_desc in AC_COUPLED_NUMBER_DESCRIPTIONS:
        if not _include_number(number_desc, inverter):
            _remove_stale_control(registry, serial, "number", number_desc.key)
    for select_desc in SELECT_DESCRIPTIONS:
        if not _include_select(select_desc, inverter):
            _remove_stale_control(registry, serial, "select", select_desc.key)
    for time_desc in TIME_DESCRIPTIONS:
        if not _include_time(time_desc, inverter):
            _remove_stale_control(registry, serial, "time", time_desc.key)
    # The System Time datetime (HR35-40) follows the same readability gate — remove
    # its row when the clock register is absent on this device/firmware (#219).
    if inverter.system_time is None:
        _remove_stale_control(registry, serial, "datetime", SYSTEM_TIME_DESCRIPTION.key)


def _reconcile_ac_coupled_dc_limits(
    hass: HomeAssistant, coordinator: GivEnergyUpdateCoordinator
) -> None:
    """Remove the DC battery-limit rows on AC-coupled / AIO plants (#52).

    Battery power on these plants is controlled via the AC pair (HR313/314); the
    number platform suppresses the DC pair (HR111/112) there. But on an upgraded
    entry HA keeps the pre-existing DC rows when the platform stops adding them, so
    the bundled dashboard would still resolve the now-orphaned DC controls from the
    registry. Mirror the other reconcilers and remove exactly those rows pre-platform.
    DC-coupled hybrids don't enter this branch and keep their DC controls.
    """
    # Local import: the platform imports from this package, so a module-scope import
    # risks a load-order cycle. The key set is the same one the platform suppresses on.
    from .number import _DC_BATTERY_LIMIT_KEYS

    # The gate is structural (plant capability), not a register read, so — like the
    # EMS reconciliation — it needs no partial-poll guard: a None/incomplete
    # capabilities simply fails the positive check and removes nothing.
    caps = coordinator.data.capabilities
    if caps is None or not caps.has_ac_config_block or caps.is_three_phase:
        return
    serial = coordinator.data.inverter_serial_number
    registry = er.async_get(hass)
    for key in _DC_BATTERY_LIMIT_KEYS:
        entity_id = registry.async_get_entity_id("number", DOMAIN, f"{serial}_{key}")
        if entity_id is not None:
            _LOGGER.info(
                "Removing DC control %s: AC-coupled plant uses the AC pair (#52)", entity_id
            )
            registry.async_remove(entity_id)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (registered as an update listener)."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _check_system_time_drift(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: GivEnergyUpdateCoordinator
) -> None:
    """Raise/clear a repair when the inverter clock drifts from HA's time (#219).

    Runs each coordinator update (and once at setup). Idempotent: re-raising an
    existing issue updates it; deleting an absent one is a no-op. Each guard clears
    any standing issue before returning so a now-resolved condition self-heals.
    """
    issue_id = f"system_time_drift_{entry.entry_id}"

    def clear() -> None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    # Opt-out for users who deliberately run the inverter in another zone (e.g. UTC),
    # and skip battery-data-only entries (no clock surfaced — mirrors datetime setup).
    if not entry.options.get(CONF_WARN_CLOCK_DRIFT, DEFAULT_WARN_CLOCK_DRIFT):
        clear()
        return
    if entry.options.get(CONF_BATTERY_DATA_ONLY, DEFAULT_BATTERY_DATA_ONLY):
        clear()
        return

    system_time = coordinator.data.inverter.system_time
    now = dt_util.now()
    drift = system_time_drift(system_time, now)
    # drift is None when the clock register reads None (transient) — don't fire.
    if drift is None or drift < SYSTEM_TIME_DRIFT_THRESHOLD:
        clear()
        return

    placeholders = {
        "drift_minutes": str(int(drift.total_seconds() // 60)),
        "system_time": system_time.strftime("%Y-%m-%d %H:%M"),
        "ha_time": now.strftime("%Y-%m-%d %H:%M"),
    }
    # On an EMS plant the controller's clock can't be set locally: the modbus library
    # models the controller as a non-inverter peer and refuses HR(35) writes, so the
    # one-click fix would error. Surface the drift as a non-fixable notice that points
    # to the GivEnergy app instead (the controller normally re-syncs from the cloud).
    # Reading the clock still works, so detection — what Predbat consumes via the
    # System Time entity — is unaffected.
    is_ems = coordinator.data.ems is not None
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=not is_ems,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="system_time_drift_ems" if is_ems else "system_time_drift",
        translation_placeholders=placeholders,
        data={"entry_id": entry.entry_id, **placeholders},
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Clear any legacy dashboard_outdated issues left by the now-removed
    # generate_dashboard service so the Repairs UI doesn't show a broken Fix button.
    # Issues were versioned (dashboard_outdated_v1 … dashboard_outdated_v11).
    for _v in range(1, 12):
        ir.async_delete_issue(hass, DOMAIN, f"dashboard_outdated_v{_v}")

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

    async def _on_devices_missing(prior: PlantCapabilities, actual: PlantCapabilities) -> None:
        # A previously-known device stopped responding and the loss persisted
        # across retries. Do NOT persist the reduced topology and do NOT reload —
        # the full prior stays cached so the next reconnect re-probes it and can
        # self-heal. Raise a loud, fixable repair so a human decides whether this
        # is transient or a genuine removal.
        devices = ", ".join(missing_devices(prior, actual)) or "a device"
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"expected_devices_missing_{entry.entry_id}",
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="expected_devices_missing",
            translation_placeholders={"devices": devices},
            data={"entry_id": entry.entry_id, "devices": devices},
        )

    # Capabilities snapshot the platforms built their entities from — captured
    # after async_forward_entry_setups below. None until then, so the heal that
    # fires during the initial connect (before any entities exist) is a no-op.
    setup_capabilities: PlantCapabilities | None = None

    async def _on_topology_healed(confirmed: PlantCapabilities) -> None:
        # Full expected topology confirmed — clear any standing "device missing"
        # repair the moment the device answers again (idempotent: a no-op when
        # no such issue exists).
        ir.async_delete_issue(hass, DOMAIN, f"expected_devices_missing_{entry.entry_id}")
        # Entity sets are frozen at platform setup: a device that answered late
        # (slow BMS during a warm-start detect) got no entities, and nothing
        # creates them when it recovers. missing_devices(confirmed, setup)
        # lists exactly those — present in the confirmed topology, absent from
        # the snapshot entities were built from — so reload to pick them up (#148).
        if setup_capabilities is None:
            return
        appeared = missing_devices(confirmed, setup_capabilities)
        if appeared:
            _LOGGER.info(
                "Recovered device(s) %s had no entities created at setup; "
                "reloading the entry to create them",
                ", ".join(appeared),
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)

    # Resolve opt-in experimental client flags from options into Client(...) kwargs.
    # Empty for the default-off case, so the construction is unchanged.
    experimental_client_kwargs = resolve_experimental_client_kwargs(entry.options)

    coordinator = GivEnergyUpdateCoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        passive=entry.data.get(CONF_PASSIVE, DEFAULT_PASSIVE),
        experimental_client_kwargs=experimental_client_kwargs,
        prior_capabilities=prior_capabilities,
        on_topology_changed=_on_topology_changed,
        on_devices_missing=_on_devices_missing,
        on_topology_healed=_on_topology_healed,
    )

    await coordinator.async_config_entry_first_refresh()

    # Seed the cache on cold start (no prior loaded), and FRESHEN it on a warm
    # hit whose live capabilities drifted from the cache: detect() rebuilds the
    # capabilities from the wire, so derived fields can legitimately change
    # under us — givenergy-modbus 2.3.0's 0x31 read-alias retirement (#249) is
    # the motivating case, where a persisted inverter_address=0x31 works only
    # via the hardware facade and is expected to self-heal by the consumer
    # re-persisting after detect(). The matching warm hit stays write-free.
    # Mismatch is already covered by _on_topology_changed having saved exc.actual.
    #
    # Only persist a CLEAN poll: if the seed poll was partial (last_partial_failures
    # non-empty), the integration still loads (coordinator serves the partial), but
    # we don't commit a possibly-degraded topology to disk — flaky kit could
    # otherwise vanish permanently on the next warm start. A permanently-partial
    # plant re-detects fresh each cold start and self-heals to a clean persist once
    # the read succeeds.
    live_capabilities = coordinator.data.capabilities
    if live_capabilities is not None and not coordinator.last_partial_failures:
        if prior_capabilities is None:
            await _save_capabilities(hass, entry.entry_id, live_capabilities)
        elif live_capabilities != prior_capabilities and not missing_devices(
            prior_capabilities, live_capabilities
        ):
            # Never freshen with a loss-reduced topology: after a persistent
            # loss the served capabilities are deliberately reduced for the
            # tick while the full prior stays cached for the next re-probe.
            await _save_capabilities(hass, entry.entry_id, live_capabilities)
            # Reconnect detect() hints should follow the wire too, not the
            # stale cache we loaded at startup.
            coordinator._prior_capabilities = live_capabilities

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Reload the entry when its options change (e.g. the battery-data-only toggle,
    # #95), so the platforms re-enumerate with the new filter. No listener exists
    # otherwise, so an options change would have no effect until a manual reload.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Watch the inverter clock each poll and raise a repair when it drifts (#219).
    @callback
    def _system_time_drift_listener() -> None:
        _check_system_time_drift(hass, entry, coordinator)

    entry.async_on_unload(coordinator.async_add_listener(_system_time_drift_listener))
    _system_time_drift_listener()  # evaluate immediately rather than after one poll

    # Re-point any entities under renamed unique_ids before the platforms create
    # them, so the existing entity (and its history) is reused rather than orphaned.
    _migrate_unique_ids(hass, entry)

    # On an EMS plant the 0x11 device is a controller, not an inverter (#201). An
    # upgraded entry still carries the inverter sensors/controls an earlier version
    # created; remove them so the controller falls to its lean EMS set instead of
    # keeping them as orphaned/unavailable rows. Fresh installs match nothing.
    if coordinator.data.ems is not None:
        _reconcile_ems_entities(hass, entry, coordinator.data.inverter_serial_number)

    # Remove control rows whose register is absent on this device/firmware (#207):
    # the platforms skip creating them, but an upgraded entry keeps the stale row.
    _reconcile_readability_gated_controls(hass, coordinator)

    # On AC-coupled / AIO plants the DC battery-limit controls are suppressed in
    # favour of the AC pair; remove the stale DC rows an upgraded entry would keep (#52).
    _reconcile_ac_coupled_dc_limits(hass, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The platforms have now enumerated their entities from coordinator.data —
    # snapshot the topology that enumeration saw for the heal-path diff (#148).
    setup_capabilities = coordinator.data.capabilities

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
                await hass.async_add_executor_job(
                    partial(directory.mkdir, mode=0o700, exist_ok=True)
                )
                await hass.async_add_executor_job(write_capture, directory / filename, content)

                landing_url = build_capture_notification_url(hass, filename)
                _LOGGER.info(
                    "GivEnergy frame capture saved to %s (%d frames)", filename, len(frames)
                )
                # Raw <a target="_blank"> rather than a markdown link: the HA
                # frontend's SPA router hijacks same-origin markdown-link clicks
                # into in-app navigation, so an `/api/...` link lands on the
                # dashboard instead of opening the capture. A truthy `target`
                # is the one attribute that survives notification markdown
                # sanitisation *and* makes the router skip the click, letting the
                # browser navigate to the backend view (resolved against the
                # user's actual origin, proxy included).
                async_create_notification(
                    hass,
                    (
                        f"Captured {len(frames)} frames over {duration} s.\n\n"
                        f'<a href="{landing_url}" target="_blank">Open the capture</a> '
                        "to inspect it, download the file, or open a pre-filled "
                        "GitHub issue."
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

        async def handle_redetect_plant(call: ServiceCall) -> None:
            # Clear the cached topology for this device's entry and trigger a
            # reload — the next setup_entry will see no prior and do a cold
            # detect(), which is exactly the "I changed the hardware, please
            # rediscover" semantic the issue calls for.
            target_entry_id, err = _resolve_target(hass, call.data)
            if err or target_entry_id is None:
                raise HomeAssistantError(err or "Could not resolve target entry")
            await _redetect_plant_entry(hass, target_entry_id)

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
        # Shut down before discarding the client: no new scheduled refresh can
        # race the teardown. HA auto-registers async_shutdown via
        # config_entry.async_on_unload, but those callbacks fire only after this
        # function returns — too late, the client would already be gone. An
        # already-in-flight refresh isn't cancelled by either path; the
        # coordinator's loss-retry loop guards against the discarded client.
        await coordinator.async_shutdown()
        await coordinator.async_close()

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_REBOOT_INVERTER)
        hass.services.async_remove(DOMAIN, SERVICE_CALIBRATE_BATTERY_SOC)
        hass.services.async_remove(DOMAIN, SERVICE_SET_SYSTEM_DATETIME)
        hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_FRAMES)
        hass.services.async_remove(DOMAIN, SERVICE_REDETECT_PLANT)
        hass.services.async_remove(DOMAIN, SERVICE_EXPOSE_RECOMMENDED_ENTITIES)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Discard the entry's cached plant-capabilities store when it is deleted.

    HA does not auto-remove an integration's `Store` data on config-entry
    deletion, so without this a delete-then-re-add (or a leftover after an
    uninstall) would orphan the `.storage` capabilities file. The cache is keyed
    by entry_id, so a re-added entry gets a fresh id and a cold detect() either
    way — this is housekeeping, not a behaviour change.
    """
    await _capabilities_store(hass, entry.entry_id).async_remove()
