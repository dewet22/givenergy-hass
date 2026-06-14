from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

_MIGRATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_from_givtcp.py"

_LONDON = ZoneInfo("Europe/London")


def _load_migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_from_givtcp", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_migrate_module()


def test_classify_entity_by_suffix():
    rc = _MOD.ResetClass
    assert _MOD.classify_entity("pv_energy_today") is rc.DAILY
    assert _MOD.classify_entity("house_consumption_today") is rc.DAILY
    assert _MOD.classify_entity("pv_generation_total") is rc.LIFETIME
    assert _MOD.classify_entity("grid_import_total") is rc.LIFETIME
    assert _MOD.classify_entity("battery_discharge_this_year") is rc.ANNUAL


def test_adaptive_ceiling_rejects_fakes_keeps_genuine():
    # Genuine PV-like hourly deltas (0–6 kWh) with a few huge fake spikes.
    genuine = [0.1, 0.5, 1.2, 2.0, 3.5, 5.0, 6.0, 0.7, 0.3, 4.4] * 30
    fakes = [27396.1, 29671.9, 28660.0]
    ceiling = _MOD.adaptive_ceiling(genuine + fakes)
    assert max(genuine) <= ceiling < 100.0  # genuine peaks pass; fakes far above


def test_adaptive_ceiling_no_positive_deltas_is_unbounded():
    assert _MOD.adaptive_ceiling([0.0, 0.0, None]) == float("inf")


def test_reset_boundary_daily_local_midnight():
    rc = _MOD.ResetClass
    # BST: local midnight is 23:00Z. Within tolerance -> reset boundary.
    assert _MOD._is_reset_boundary("2026-05-20T23:00:00+00:00", rc.DAILY, _LONDON, 2.0)
    # Mid-afternoon -> not a reset boundary (off-midnight corruption).
    assert not _MOD._is_reset_boundary("2026-05-20T14:00:00+00:00", rc.DAILY, _LONDON, 2.0)


def test_reset_boundary_lifetime_never():
    rc = _MOD.ResetClass
    assert not _MOD._is_reset_boundary("2026-01-01T00:00:00+00:00", rc.LIFETIME, _LONDON, 2.0)


def test_reset_boundary_annual_year_start():
    rc = _MOD.ResetClass
    # GMT: 2026-01-01 00:00 local == 00:00Z.
    assert _MOD._is_reset_boundary("2026-01-01T00:00:00+00:00", rc.ANNUAL, _LONDON, 2.0)
    assert not _MOD._is_reset_boundary("2026-06-15T00:00:00+00:00", rc.ANNUAL, _LONDON, 2.0)
