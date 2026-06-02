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
