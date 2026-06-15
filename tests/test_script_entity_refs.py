"""Guard the out-of-tree helper scripts against entity renames.

``scripts/migrate_from_givtcp.py`` bakes in givenergy_local entity-ID slugs.
An entity rename that lands without updating it would silently target statistics
IDs that don't exist. These tests fail loudly in that case by checking every
referenced entity against a live registry.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify as ha_slugify

# The shared fixtures register one inverter (SA1234G123) and one battery
# (BT1234A001); entity IDs lowercase the serial.
INV = "sa1234g123"
BATT = "bt1234a001"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATE_SCRIPT = _REPO_ROOT / "scripts" / "migrate_from_givtcp.py"


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
        self.calls: list[dict] = []

    async def get_statistics(self, ids, start, end=None, types=None):  # noqa: ANN001 - mirrors real sig
        self.calls.append({"ids": list(ids), "types": types})
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
_TZ = ZoneInfo("Europe/London")


def test_systemic_resolution_failure_uses_registry_recognition():
    """The pre-flight abort keys on registry recognition, not recorder presence.

    A partial miss (one target unrecognised, another recognised) is not systemic,
    so the migration proceeds and the miss is skipped per-entity. But if NOT ONE
    target was recognised it aborts — even when an orphan from a prior broken
    --apply sits in the recorder under the canonical id (which this check, keyed
    only on the plan's registry-recognition flag, never consults).
    """
    mod = _load_migrate_module()
    # plan tuple: (givtcp_id, ge_id, desc, unit, warn, reset_class, resolved)
    rc = mod.ResetClass.LIFETIME
    recognised = (
        "sensor.givtcp_x_import",
        "sensor.loft_x_import",
        "Import",
        "kWh",
        False,
        rc,
        True,
    )
    unresolved = ("sensor.givtcp_x_pv", "sensor.ge_x_pv", "PV", "kWh", False, rc, False)

    # At least one target recognised -> not systemic -> no abort.
    assert mod._systemic_resolution_failure([recognised, unresolved]) == []
    # Nothing recognised -> systemic -> surface every target, regardless of any
    # orphan the recorder may hold for these ids.
    assert mod._systemic_resolution_failure([unresolved]) == ["sensor.ge_x_pv"]


async def test_migrate_entity_refuses_unknown_ge_target():
    """A resolved GE target that isn't a real recorder statistic must be skipped.

    This is the safety net for the area-prefix/rename failure mode: if resolution
    produces a phantom id (`ge_known=False`), migrate_entity must refuse rather
    than clear-and-import an orphan series, leaving the real entity un-migrated.
    """
    mod = _load_migrate_module()
    ws = _FakeWS({_GIVTCP: [_stat_row(datetime(2026, 6, 5, tzinfo=UTC), 100.0)]})
    r = await mod.migrate_entity(
        ws,
        _GIVTCP,
        _GE,
        "Solar generation today",
        _CUTOVER,
        "kWh",
        False,
        ge_known=False,
        reset_class=mod.ResetClass.DAILY,
        tz=_TZ,
        trust_source_sums=False,
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
        ws,
        _GIVTCP,
        _GE,
        "Solar generation today",
        _CUTOVER,
        "kWh",
        False,
        ge_known=True,
        reset_class=mod.ResetClass.DAILY,
        tz=_TZ,
        trust_source_sums=False,
    )
    assert r.status == "dry_run"
    assert (r.ge_pre_rows, r.ge_post_rows) == (1, 1)
    assert r.warn_no_ge_pre is False
    # The sum path must read the default sum/state series — it must NOT request
    # mean/min/max (that's the mean path's contract). get_statistics passes no
    # `types`, so every recorded call has types=None; assert the sum path never
    # asked for mean kinds.
    assert ws.calls, "migrate_entity made no get_statistics calls"
    assert all(c["types"] is None for c in ws.calls)
    assert all(c["types"] != ["mean", "min", "max"] for c in ws.calls)


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
        ws,
        _GIVTCP,
        _GE,
        "Solar generation today",
        _CUTOVER,
        "kWh",
        False,
        ge_known=True,
        reset_class=mod.ResetClass.DAILY,
        tz=_TZ,
        trust_source_sums=False,
    )
    assert r.status == "dry_run"
    assert r.ge_pre_rows == 0
    assert r.warn_no_ge_pre is True


async def test_migrate_entity_trust_source_sums_legacy_path():
    """trust_source_sums=True copies GivTCP sums and rebases GE-post from them.

    The escape hatch (--trust-source-sums flag) must use the GivTCP last sum as
    the rebase anchor rather than a rebuilt-from-state value, so sum_at_cutover
    equals the last GivTCP sum row directly.
    """
    mod = _load_migrate_module()
    ws = _FakeWS(
        {
            _GIVTCP: [
                _stat_row(datetime(2026, 6, 5, tzinfo=UTC), 100.0),
                _stat_row(datetime(2026, 6, 6, tzinfo=UTC), 115.0),
            ],
            _GE: [
                _stat_row(datetime(2026, 6, 5, tzinfo=UTC), 5.0),
                _stat_row(datetime(2026, 6, 7, 1, tzinfo=UTC), 6.0),
            ],
        }
    )
    r = await mod.migrate_entity(
        ws,
        _GIVTCP,
        _GE,
        "Solar generation today",
        _CUTOVER,
        "kWh",
        False,
        ge_known=True,
        reset_class=mod.ResetClass.DAILY,
        tz=_TZ,
        trust_source_sums=True,
    )
    assert r.status == "dry_run"
    # Legacy path: sum_at_cutover is exactly the last GivTCP sum row, not a
    # state-derived figure.
    assert r.sum_at_cutover == 115.0
    # merged = 2 givtcp rows + 1 ge_post row (the pre-cutover GE row is dropped)
    assert r.merged_rows == 3


_GIVTCP_MEAN = "sensor.givtcp_x_pv_power"
_GE_MEAN = "sensor.givenergy_inverter_x_pv_power"


def _mean_row(dt: datetime, mean: float) -> dict:
    """A recorder statistics mean row (millisecond `start`, mean/min/max fields)."""
    return {
        "start": int(dt.timestamp() * 1000),
        "mean": mean,
        "min": mean * 0.8,
        "max": mean * 1.2,
    }


async def test_migrate_mean_entity_dry_run():
    """migrate_mean_entity previews a mean-type series without touching HA.

    Provides GivTCP mean rows pre-cutover and a GE target series post-cutover;
    asserts status=dry_run and that merged_rows equals givtcp_pre + ge_post.
    """
    mod = _load_migrate_module()
    givtcp_pre = [
        _mean_row(datetime(2026, 6, 5, tzinfo=UTC), 1500.0),
        _mean_row(datetime(2026, 6, 6, tzinfo=UTC), 1200.0),
    ]
    # A GivTCP row exactly at the cutover boundary must be excluded (< cutover).
    givtcp_at_cutover = [_mean_row(_CUTOVER, 1300.0)]
    ge_post = [
        _mean_row(datetime(2026, 6, 7, 1, tzinfo=UTC), 1100.0),
        _mean_row(datetime(2026, 6, 8, tzinfo=UTC), 950.0),
    ]
    # Add a GE pre-cutover row that should be excluded from merged output
    ge_pre = [_mean_row(datetime(2026, 6, 5, tzinfo=UTC), 200.0)]
    ws = _FakeWS(
        {
            _GIVTCP_MEAN: givtcp_pre + givtcp_at_cutover,
            _GE_MEAN: ge_pre + ge_post,
        }
    )
    r = await mod.migrate_mean_entity(
        ws,
        _GIVTCP_MEAN,
        _GE_MEAN,
        "PV power",
        _CUTOVER,
        "W",
        False,
        ge_known=True,
    )
    assert r.status == "dry_run"
    # merged = 2 givtcp pre-cutover rows + 2 ge post-cutover rows; the boundary
    # GivTCP row at exactly cutover is dropped by the < cutover filter.
    assert r.merged_rows == len(givtcp_pre) + len(ge_post)
    # The mean path must request mean/min/max — not the sum path's default kinds.
    assert ws.calls, "migrate_mean_entity made no get_statistics calls"
    assert any(c["types"] == ["mean", "min", "max"] for c in ws.calls)
