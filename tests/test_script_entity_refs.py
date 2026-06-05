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
import re
from pathlib import Path

from homeassistant.helpers import entity_registry as er

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


def _inverter_pairs_from_source() -> list[tuple]:
    """Extract INVERTER_PAIRS from the migrate script without importing it.

    The script imports ``websockets`` at module load (sys.exit if absent), so a
    direct import isn't viable in CI. The mapping is a pure literal, so parse it
    out of the AST instead.
    """
    tree = ast.parse(_MIGRATE_SCRIPT.read_text())
    for node in tree.body:
        # The script declares it as an annotated assignment:
        #   INVERTER_PAIRS: list[tuple[...]] = [...]
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == "INVERTER_PAIRS" for t in targets):
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError("INVERTER_PAIRS not found in migrate_from_givtcp.py")


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
    pairs = _inverter_pairs_from_source()
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
