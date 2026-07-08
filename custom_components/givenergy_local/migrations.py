"""Entity-registry migrations and reconciliations.

House policy: every function here is a dated, gated correction to registry
state — a unique_id rename carrying history across a semantic fix, or the
removal of rows an older version created that the current version no longer
does. Each carries an `Introduced:` header (version, date, issue) and a
`Removal candidate:` criterion; they are all expected to be DELETED once their
upgrade cohort is presumed extinct. Add new entries here, never in __init__.

All run pre-platform from async_setup_entry (order: unique_id migrations
first, then model/option-gated reconciliations), so renamed rows are adopted
and removed rows are not recreated when the platforms enumerate.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_EXPOSE_PER_CELL, DOMAIN
from .coordinator import GivEnergyUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

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

    Introduced: v1.1.x era (2026-06, modbus 2.1.1/2.1.2 renames #174/#176);
    p_grid_out entry added v1.3.x (#52). Removal candidate: when installs
    predating v1.3.31 are presumed extinct — retire map entries individually.

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

    Introduced: v1.3.11, narrowed v1.3.12 (#206, 2026-06). Removal candidate:
    when upgrades from pre-v1.3.12 EMS installs are presumed extinct.

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
    from .sensor import INVERTER_SENSORS

    keys = _inverter_control_keys()
    # Inverter sensors gated on EMS (controller-local load + Battery Charge/Discharge Today).
    keys.update(d.key for d in INVERTER_SENSORS if d.skip_if_ems)
    # Duplicate EMS aggregate dropped in favour of the retained inverter Status sensor.
    keys.add("ems_status")
    return {f"{serial}_{key}" for key in keys}


def _inverter_control_keys() -> set[str]:
    """Keys of every inverter-level control description across the platforms.

    Local imports: these platform modules are imported by the platform setup
    anyway, and importing them at module scope here risks a load-order cycle.
    """
    from .number import AC_COUPLED_NUMBER_DESCRIPTIONS, NUMBER_DESCRIPTIONS
    from .select import AC_COUPLED_SELECT_DESCRIPTIONS, SELECT_DESCRIPTIONS
    from .switch import AC_COUPLED_SWITCH_DESCRIPTIONS, SWITCH_DESCRIPTIONS
    from .time import SMART_LOAD_TIME_DESCRIPTIONS, TIME_DESCRIPTIONS

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
    return {d.key for d in controls}


def _retired_on_gateway_unique_ids(serial: str) -> set[str]:
    """Unique IDs of the entities retired on a Gateway plant (#194).

    Introduced: v1.3.39 (2026-07-03); shared-key exclusion added 2026-07-08
    (#266). Removal candidate: when upgrades from pre-v1.3.39 Gateway entries
    are presumed extinct — but note the suppression itself is permanent.

    On a Gateway the 0x11 device's inverter registers decode as a spurious
    all-zeros SinglePhaseInverter, so the ENTIRE standard inverter sensor set is
    suppressed (unlike EMS, where the 0x11 block carries real data) along with
    every inverter-level control (no validated write surface yet). The
    GATEWAY_SENSORS set replaces them on the same device; coordinator sensors
    use different keys and stay.
    """
    from .sensor import GATEWAY_SENSORS, INVERTER_SENSORS

    keys = _inverter_control_keys()
    # Five keys are shared between the sets (p_pv and the pv/load/battery
    # lifetime totals): the gateway sensors deliberately reuse them for
    # continuity, so retiring every inverter key would delete those live
    # gateway rows on EVERY reload (shipped briefly in v1.3.39/40).
    gateway_keys = {d.key for d in GATEWAY_SENSORS}
    keys.update(d.key for d in INVERTER_SENSORS if d.key not in gateway_keys)
    return {f"{serial}_{key}" for key in keys}


def _reconcile_gateway_entities(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """Remove standard inverter entities from a Gateway entry (#194).

    Introduced: v1.3.39 (2026-07-03). Ongoing while Gateway suppression exists
    (cheap no-op once rows are gone).

    A Gateway entry created on v1.3.38 (before GATEWAY_SENSORS existed) carries
    the full inverter sensor/control set as 0/Unknown registry rows; suppressing
    creation alone would leave them orphaned on upgrade.
    """
    registry = er.async_get(hass)
    retired = _retired_on_gateway_unique_ids(serial)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id in retired:
            _LOGGER.info(
                "Removing inverter entity %s retired on Gateway plant (#194)", ent.entity_id
            )
            registry.async_remove(ent.entity_id)


def _reconcile_ems_entities(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """Remove inverter-level entities retired on an EMS plant (#201).

    Introduced: v1.3.11/12 (2026-06). Ongoing while EMS suppression exists
    (cheap no-op once rows are gone).

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


def _reconcile_aio_house_consumption(hass: HomeAssistant, serial: str) -> None:
    """Remove the derived House Consumption Today row on an AIO entry (#95).

    Introduced: v1.3.35 (2026-07-02, #250; #293-evidence-confirmed). Removal
    candidate: when upgrades from pre-v1.3.35 AIO installs are presumed extinct.

    The sensor is gated off AIO — its PV/grid inputs are the registers whose
    identity is under investigation upstream (modbus#293), and AIO units report
    no raw consumption figure — but an upgraded entry keeps the stale registry
    row when the platform stops creating it.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_e_consumption_today")
    if entity_id is not None:
        _LOGGER.info("Removing %s: derived consumption is gated off AIO (#95)", entity_id)
        registry.async_remove(entity_id)


# The PV Generation → Inverter Output rename on Model.AC / ALL_IN_ONE, plus the
# derived rows that lose their inputs there. Values: (old_key, new_key) pairs
# share the entity_id slug shapes (pv_generation_x → inverter_output_x).
_AC_AIO_PV_RENAMES = (
    ("e_pv_generation_today", "inverter_output_today", "pv_generation_today"),
    ("e_pv_generation_total", "inverter_output_total", "pv_generation_total"),
)
_AC_AIO_STALE_DERIVED_KEYS = (
    "e_self_consumption_today",
    "e_self_consumption_total",
    "e_pv_direct_today",
)


def _migrate_ac_aio_pv_generation(hass: HomeAssistant, serial: str, model: object) -> None:
    """Rename PV Generation → Inverter Output on Model.AC / ALL_IN_ONE (2.10.0).

    Introduced: v1.3.41 (2026-07-08) — givenergy-modbus 2.10.0 (#293 manifest,
    Slice A). Removal candidate: once AC/AIO installs predating v1.3.41 are
    presumed extinct (AC/AIO support itself only shipped 2026-06, so the cohort
    is small).

    e_pv_generation_* honestly return None on those models — IR44/45-46 were
    never PV there, they're the unit's battery-discharge AC output, now carried
    by e_inverter_out_* and surfaced as the Inverter Output pair. The recorded
    history under the PV name is genuine same-register data, so rename the rows
    in place (unique_id + entity_id slug, collision-safe) and let the new
    sensors adopt them. The derived rows (Self Consumption pair, PV Direct —
    and House Consumption on Model.AC, where no other reconciler covers it)
    lose their inputs by upstream None-propagation and have no successor:
    remove them.
    """
    from givenergy_modbus.model.inverter import Model

    registry = er.async_get(hass)
    for old_key, new_key, old_slug in _AC_AIO_PV_RENAMES:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_{old_key}")
        if entity_id is None:
            continue
        if registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_{new_key}"):
            _LOGGER.debug("Skipping %s rename: target %s exists", entity_id, new_key)
            continue
        updates: _EntityUpdates = {"new_unique_id": f"{serial}_{new_key}"}
        if entity_id.endswith(f"_{old_slug}"):
            new_entity_id = entity_id[: -len(old_slug)] + new_key
            if registry.async_get(new_entity_id) is None:
                updates["new_entity_id"] = new_entity_id
        _LOGGER.info("Migrating %s in place (AC/AIO, 2.10.0): %s", entity_id, updates)
        registry.async_update_entity(entity_id, **updates)

    stale: tuple[str, ...] = _AC_AIO_STALE_DERIVED_KEYS
    if model is Model.AC:
        # ALL_IN_ONE's consumption row is handled by _reconcile_aio_house_consumption.
        stale = (*stale, "e_consumption_today")
    for key in stale:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_{key}")
        if entity_id is not None:
            _LOGGER.info(
                "Removing %s: derived field lost its PV input on AC/AIO (2.10.0)", entity_id
            )
            registry.async_remove(entity_id)


def _remove_stale_control(registry: er.EntityRegistry, serial: str, domain: str, key: str) -> None:
    """Remove a readability-gated control's stale registry row, if present (#207).

    Introduced: v1.3.13 (2026-06). Ongoing — the readability gate is permanent.
    """
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

    Introduced: v1.3.13 (2026-06). Ongoing — the readability gate is permanent.

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


# Substrings identifying a per-cell entity's unique_id (gated by
# CONF_EXPOSE_PER_CELL). Roll-ups (`cell_voltage_min`, …) and the `v_cells_sum`
# aggregate deliberately don't match — only the individual per-cell rows do.
_PER_CELL_UNIQUE_ID_MARKERS = ("_v_cell_", "_t_cell_", "_t_cells_")


def _reconcile_per_cell_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove per-cell entity rows when per-cell exposure is off (#179).

    Introduced: v1.3.30 (2026-06-29). Ongoing — option-driven cleanup, not a
    dated migration; permanent while CONF_EXPOSE_PER_CELL exists.

    The sensor platform stops creating the individual per-cell voltage/temperature
    entities when CONF_EXPOSE_PER_CELL is False, but HA keeps the pre-existing rows
    on an entry that previously had them — so turning the option off would leave
    orphaned, unavailable cell entities. Remove exactly those (matched by the
    per-cell unique_id markers), leaving the roll-ups and every other entity intact.
    Absent ⇒ legacy ⇒ on, so existing installs keep their cells untouched.
    """
    if entry.options.get(CONF_EXPOSE_PER_CELL, True):
        return
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id and any(m in ent.unique_id for m in _PER_CELL_UNIQUE_ID_MARKERS):
            _LOGGER.info(
                "Removing per-cell entity %s (per-cell exposure disabled, #179)",
                ent.entity_id,
            )
            registry.async_remove(ent.entity_id)


def _reconcile_ac_coupled_dc_limits(
    hass: HomeAssistant, coordinator: GivEnergyUpdateCoordinator
) -> None:
    """Remove the DC battery-limit rows on AC-coupled / AIO plants (#52).

    Introduced: v1.3.17 (2026-06). Ongoing while the AC-pair suppression exists
    (cheap no-op once rows are gone).

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
