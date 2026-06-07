"""Guard the out-of-tree helper scripts against entity renames.

Neither the dashboard generator (``dashboard/generate.py`` →
``generate_dashboard``) nor the GivTCP history copier
(``scripts/migrate_from_givtcp.py``) is otherwise exercised by CI, yet both bake
in givenergy_local entity-ID slugs. An entity rename that lands without updating
them would silently leave the dashboard pointing at missing entities, or the
migration writing to statistics IDs that don't exist. These tests fail loudly in
that case by checking every referenced entity against a live registry.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify as ha_slugify

from custom_components.givenergy_local.dashboard import generate_dashboard

# The shared fixtures register one inverter (SA1234G123) and one battery
# (BT1234A001); entity IDs lowercase the serial.
INV = "sa1234g123"
BATT = "bt1234a001"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATE_SCRIPT = _REPO_ROOT / "scripts" / "migrate_from_givtcp.py"

# Domains the dashboard can reference for givenergy_local entities.
_ENTITY_REF = re.compile(
    r"(?:sensor|binary_sensor|number|select|switch|time|button|update)"
    r"\.givenergy_[a-z0-9_]+"
)

# Any entity id, including the area-prefixed forms the resolver emits (which the
# canonical givenergy_-only _ENTITY_REF above would miss).
_ANY_REF = re.compile(
    r"\b(?:sensor|binary_sensor|number|select|switch|time|button|update)\.[a-z0-9_]+"
)


def _registered_entity_ids(hass) -> set[str]:
    return {e.entity_id for e in er.async_get(hass).entities.values()}


def _load_migrate_module() -> ModuleType:
    """Import the migrate script as a module.

    Its `websockets` import is lazy (deferred to HAWebSocket.connect), so the
    module imports cleanly without the dependency, exposing its pure helpers
    (`_slugify`, `build_entity_id_resolver`, the *_PAIRS tables) for direct test.
    """
    spec = importlib.util.spec_from_file_location("migrate_from_givtcp", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeWS:
    """Stand-in for HAWebSocket.get_statistics in migrate_entity guard tests."""

    def __init__(self, series: dict[str, list[dict]]) -> None:
        self._series = series

    async def get_statistics(self, ids, start, end=None):  # noqa: ANN001 - mirrors real sig
        return {i: self._series.get(i, []) for i in ids}


def _stat_row(dt: datetime, value: float) -> dict:
    """A recorder statistics row (millisecond `start`, as HA returns)."""
    return {"start": int(dt.timestamp() * 1000), "sum": value, "state": value}


def _pairs_from_source(var_name: str) -> list[tuple]:
    """Extract a ``*_PAIRS`` literal from the migrate script without importing it.

    The script imports ``websockets`` at module load (sys.exit if absent), so a
    direct import isn't viable in CI. The mappings are pure literals declared as
    annotated assignments (``NAME: list[tuple[...]] = [...]``), so parse them out
    of the AST instead.
    """
    tree = ast.parse(_MIGRATE_SCRIPT.read_text())
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == var_name for t in targets):
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError(f"{var_name} not found in migrate_from_givtcp.py")


async def test_dashboard_entity_refs_all_registered(hass, setup_integration):
    """Every entity the generated dashboard references must actually exist."""
    registered = _registered_entity_ids(hass)
    dashboard = generate_dashboard(INV, [BATT])
    refs = set(_ENTITY_REF.findall(dashboard))
    assert refs, "no givenergy_local entity references found in the dashboard"
    missing = sorted(refs - registered)
    assert not missing, (
        "dashboard references entities that the integration no longer creates "
        f"(entity rename not propagated to dashboard/template.yaml?): {missing}"
    )


async def test_dashboard_resolves_renamed_entity_ids(hass, setup_integration):
    """When entity_ids diverge from the generator's canonical scheme — HA 2026.6
    prefixes them with the device area, and users can rename them — the service
    handler's resolver must remap every reference to the actual registered id.

    The test HA core predates the 2026.6 area-prefix behaviour, so simulate it by
    renaming each entity_id to add a `loft_` prefix, then assert the resolved
    dashboard points only at ids that exist.
    """
    from custom_components.givenergy_local import _build_entity_id_resolver

    reg = er.async_get(hass)
    for ent in list(er.async_entries_for_config_entry(reg, setup_integration.entry_id)):
        domain, object_id = ent.entity_id.split(".", 1)
        if object_id.startswith("givenergy_"):
            reg.async_update_entity(ent.entity_id, new_entity_id=f"{domain}.loft_{object_id}")

    resolve = _build_entity_id_resolver(hass, setup_integration.entry_id)
    dashboard = generate_dashboard(INV, [BATT], resolve_entity_id=resolve)
    # The resolved dashboard carries area-prefixed ids, which the canonical-only
    # _ENTITY_REF wouldn't spot — match any entity id here.
    refs = set(_ANY_REF.findall(dashboard))
    assert refs, "no givenergy_local entity references found in the dashboard"

    registered = _registered_entity_ids(hass)
    # The resolver must never leave a canonical (area-less) ref for an entity that
    # exists. We only assert about entities whose renamed counterpart is actually
    # registered, so the check is immune to the shared fixture occasionally not
    # registering a mock enum/diagnostic sensor (see the all_registered guard).
    leaked = {
        ref
        for ref in refs
        if not ref.split(".", 1)[1].startswith("loft_")
        and f"{ref.split('.', 1)[0]}.loft_{ref.split('.', 1)[1]}" in registered
    }
    assert not leaked, f"resolver left canonical refs for renamed entities: {sorted(leaked)}"
    # Guard against a vacuous pass: resolution must actually have happened.
    assert any(ref.split(".", 1)[1].startswith("loft_givenergy_") for ref in refs)


async def test_migrate_script_targets_all_registered(hass, setup_integration):
    """Every givenergy_local target the migrate script maps to must exist.

    The script writes statistics to ``sensor.givenergy_inverter_<sn>_<ge_suffix>``;
    if a suffix here drifts from the real entity slug the migration silently
    targets a non-existent statistics ID.
    """
    registered = _registered_entity_ids(hass)
    pairs = _pairs_from_source("INVERTER_PAIRS")
    assert pairs, "INVERTER_PAIRS is empty"
    missing = sorted(
        f"sensor.givenergy_inverter_{INV}_{ge_suffix}"
        for _givtcp, ge_suffix, *_rest in pairs
        if f"sensor.givenergy_inverter_{INV}_{ge_suffix}" not in registered
    )
    assert not missing, (
        "migrate_from_givtcp.py maps to entities the integration no longer "
        f"creates (entity rename not propagated to the script?): {missing}"
    )


async def test_migrate_script_battery_targets_all_registered(hass, setup_integration):
    """Every givenergy_local battery target the migrate script maps to must exist.

    The script writes statistics to ``sensor.givenergy_battery_<sn>_<ge_suffix>``;
    a battery suffix that drifts from the real entity slug — e.g. the description
    *key* ``num_cycles`` vs the *entity_id* slug ``charge_cycles`` (derived from
    the "Charge Cycles" name) — would silently target a non-existent statistics
    ID. ``BATTERY_PAIRS`` may legitimately be empty, in which case there is
    nothing to check.
    """
    registered = _registered_entity_ids(hass)
    pairs = _pairs_from_source("BATTERY_PAIRS")
    missing = sorted(
        f"sensor.givenergy_battery_{BATT}_{ge_suffix}"
        for _givtcp, ge_suffix, *_rest in pairs
        if f"sensor.givenergy_battery_{BATT}_{ge_suffix}" not in registered
    )
    assert not missing, (
        "migrate_from_givtcp.py maps to battery entities the integration no "
        f"longer creates (entity rename not propagated to the script?): {missing}"
    )


def test_battery_cycles_not_migrated():
    """`battery_cycles` must stay out of BATTERY_PAIRS.

    GivTCP records per-pack cycles as a *mean* statistic, but givenergy_local's
    charge_cycles is total_increasing (a *sum* series). migrate_entity rebases a
    sum column the source lacks, so migrating cycles would rebase the GE counter
    to ~0 and corrupt it. Re-adding the pair needs a bespoke mean→counter path,
    not a plain BATTERY_PAIRS entry — this guards a naive re-add (reverted #126).
    """
    mod = _load_migrate_module()
    givtcp_sources = {givtcp for givtcp, *_rest in mod.BATTERY_PAIRS}
    assert "battery_cycles" not in givtcp_sources, (
        "battery_cycles is a mean statistic and cannot be migrated onto the "
        "total_increasing charge_cycles sum series without a bespoke path"
    )


async def test_migrate_slugify_matches_ha(hass, setup_integration):
    """The script's vendored `_slugify` must match `homeassistant.util.slugify`.

    The script runs out-of-process and can't import HA's slugify, so it carries a
    small replica used to reconstruct canonical entity ids from device + sensor
    names. If the two ever diverge for a real GivEnergy name, the resolver would
    build the wrong canonical key and silently fail to remap that entity.
    """
    mod = _load_migrate_module()
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    names: set[str] = set()
    for ent in er.async_entries_for_config_entry(reg, setup_integration.entry_id):
        if ent.platform != "givenergy_local":
            continue
        if ent.original_name:
            names.add(ent.original_name)
        device = dev_reg.async_get(ent.device_id) if ent.device_id else None
        if device and device.name:
            names.add(device.name)

    assert names, "no givenergy_local device/sensor names found to check"
    mismatches = {
        name: (mod._slugify(name), ha_slugify(name))
        for name in names
        if mod._slugify(name) != ha_slugify(name)
    }
    assert not mismatches, f"_slugify diverges from homeassistant.util.slugify: {mismatches}"


async def test_migrate_resolver_maps_area_prefixed_ids(hass, setup_integration):
    """The migrate script must remap canonical targets to area-prefixed real ids.

    HA 2026.6 prefixes generated entity_ids (and therefore statistic_ids) with the
    device area — `sensor.loft_givenergy_inverter_…`. The script's hard-coded
    canonical targets (`sensor.givenergy_inverter_<sn>_<suffix>`) must resolve to
    those real ids, or `--apply` writes to phantom statistics nothing references.

    The test HA core predates the 2026.6 area-prefix behaviour, so simulate it:
    feed the resolver registry payloads whose entity_ids carry a `loft_` prefix,
    then assert every canonical target resolves to its prefixed counterpart.
    """
    mod = _load_migrate_module()
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    entity_entries: list[dict] = []
    device_entries: list[dict] = []
    seen_devices: set[str] = set()
    for ent in er.async_entries_for_config_entry(reg, setup_integration.entry_id):
        if ent.platform != "givenergy_local" or not ent.entity_id:
            continue
        domain, object_id = ent.entity_id.split(".", 1)
        entity_entries.append(
            {
                "entity_id": f"{domain}.loft_{object_id}",
                "platform": "givenergy_local",
                "device_id": ent.device_id,
                "original_name": ent.original_name,
            }
        )
        device = dev_reg.async_get(ent.device_id) if ent.device_id else None
        if device and device.id not in seen_devices:
            seen_devices.add(device.id)
            device_entries.append({"id": device.id, "name": device.name})

    resolver = mod.build_entity_id_resolver(entity_entries, device_entries)
    assert resolver, "resolver built no canonical→actual mappings"

    canonical_targets = [
        f"sensor.givenergy_inverter_{INV}_{ge_suffix}"
        for _givtcp, ge_suffix, *_rest in mod.INVERTER_PAIRS
    ] + [
        f"sensor.givenergy_battery_{BATT}_{ge_suffix}"
        for _givtcp, ge_suffix, *_rest in mod.BATTERY_PAIRS
    ]

    checked = 0
    for canonical in canonical_targets:
        # Only assert about targets whose entity is registered in the fixtures;
        # the resolver leaves unknown ids untouched (and the *_all_registered
        # guards above already catch suffix drift).
        if canonical not in resolver:
            continue
        domain, object_id = canonical.split(".", 1)
        assert resolver[canonical] == f"{domain}.loft_{object_id}"
        checked += 1

    assert checked, "no canonical targets resolved — vacuous test"


_CUTOVER = datetime(2026, 6, 7, tzinfo=UTC)
_GIVTCP = "sensor.givtcp_x_pv_energy_today_kwh"
_GE = "sensor.givenergy_inverter_x_pv_energy_today"


async def test_migrate_entity_refuses_unknown_ge_target():
    """A resolved GE target that isn't a real recorder statistic must be skipped.

    This is the safety net for the area-prefix/rename failure mode: if resolution
    produces a phantom id (`ge_known=False`), migrate_entity must refuse rather
    than clear-and-import an orphan series, leaving the real entity un-migrated.
    """
    mod = _load_migrate_module()
    ws = _FakeWS({_GIVTCP: [_stat_row(datetime(2026, 6, 5, tzinfo=UTC), 100.0)]})
    r = await mod.migrate_entity(
        ws, _GIVTCP, _GE, "Solar generation today", _CUTOVER, "kWh", False, ge_known=False
    )
    assert r.status == "ge_not_found"


async def test_migrate_entity_dry_run_with_overlap():
    """A known GE target with pre- and post-cutover data previews cleanly."""
    mod = _load_migrate_module()
    ws = _FakeWS(
        {
            _GIVTCP: [
                _stat_row(datetime(2026, 6, 5, tzinfo=UTC), 100.0),
                _stat_row(datetime(2026, 6, 6, tzinfo=UTC), 110.0),
            ],
            _GE: [
                _stat_row(datetime(2026, 6, 5, tzinfo=UTC), 5.0),
                _stat_row(datetime(2026, 6, 7, 1, tzinfo=UTC), 6.0),
            ],
        }
    )
    r = await mod.migrate_entity(
        ws, _GIVTCP, _GE, "Solar generation today", _CUTOVER, "kWh", False, ge_known=True
    )
    assert r.status == "dry_run"
    assert (r.ge_pre_rows, r.ge_post_rows) == (1, 1)
    assert r.warn_no_ge_pre is False


async def test_migrate_entity_flags_missing_overlap():
    """A known GE target with no pre-cutover history is flagged (not blocked)."""
    mod = _load_migrate_module()
    ws = _FakeWS(
        {
            _GIVTCP: [_stat_row(datetime(2026, 6, 5, tzinfo=UTC), 100.0)],
            _GE: [_stat_row(datetime(2026, 6, 7, 1, tzinfo=UTC), 6.0)],  # post-cutover only
        }
    )
    r = await mod.migrate_entity(
        ws, _GIVTCP, _GE, "Solar generation today", _CUTOVER, "kWh", False, ge_known=True
    )
    assert r.status == "dry_run"
    assert r.ge_pre_rows == 0
    assert r.warn_no_ge_pre is True
