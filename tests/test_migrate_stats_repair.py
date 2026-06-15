from __future__ import annotations

import asyncio
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


def _row(start_iso: str, state: float | None) -> dict:
    return {"start": start_iso, "state": state}


def _sums(rows: list[dict]) -> list[float]:
    return [r["sum"] for r in rows]


def test_rebuild_walk_accumulates_genuine_deltas():
    rows = [
        _row("2026-05-20T08:00:00+00:00", 100.0),
        _row("2026-05-20T09:00:00+00:00", 102.0),
        _row("2026-05-20T10:00:00+00:00", 105.0),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert _sums(out) == [100.0, 102.0, 105.0]


def test_rebuild_walk_holds_through_fake_zero_and_recovery():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 200.0),
        _row("2026-05-20T13:00:00+00:00", 0.0),  # fake zero-read
        _row("2026-05-20T14:00:00+00:00", 203.0),  # recovery
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert _sums(out) == [200.0, 200.0, 203.0]


def test_rebuild_walk_rejects_spike_over_ceiling():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 100.0),
        _row("2026-05-20T13:00:00+00:00", 27496.1),  # fake spike
        _row("2026-05-20T14:00:00+00:00", 101.0),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert _sums(out) == [100.0, 100.0, 101.0]


def test_rebuild_walk_accepts_daily_midnight_reset():
    rows = [
        _row("2026-05-20T22:00:00+00:00", 18.0),
        _row("2026-05-20T23:00:00+00:00", 0.4),  # post-reset (BST midnight)
        _row("2026-05-21T00:00:00+00:00", 0.9),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.DAILY, 10.0, _LONDON)
    assert _sums(out) == [18.0, 18.4, 18.9]


def test_rebuild_walk_rejects_offmidnight_drop_on_daily():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 8.0),
        _row("2026-05-20T13:00:00+00:00", 0.0),  # off-midnight -> corruption
        _row("2026-05-20T14:00:00+00:00", 8.3),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.DAILY, 10.0, _LONDON)
    assert _sums(out) == [8.0, 8.0, 8.3]


def test_rebuild_walk_carries_sum_across_gap_rows():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 100.0),
        _row("2026-05-20T13:00:00+00:00", None),  # gap
        _row("2026-05-20T14:00:00+00:00", 101.0),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert _sums(out) == [100.0, 100.0, 101.0]


def test_build_merged_states_concatenates_across_cutover():
    mod = _load_migrate_module()
    from datetime import UTC, datetime

    cutover = datetime(2026, 5, 20, tzinfo=UTC)
    givtcp = [
        {"start": "2026-05-19T23:00:00+00:00", "state": 2360.0},
        {"start": "2026-05-20T00:00:00+00:00", "state": 2364.0},  # at/after cutover -> dropped
    ]
    ge = [
        {"start": "2026-05-19T22:00:00+00:00", "state": 0.1},  # before cutover -> dropped
        {"start": "2026-05-20T00:00:00+00:00", "state": 0.2},
        {"start": "2026-05-20T01:00:00+00:00", "state": 0.5},
    ]
    merged = mod.build_merged_states(givtcp, ge, cutover)
    assert [r["start"] for r in merged] == [
        "2026-05-19T23:00:00+00:00",
        "2026-05-20T00:00:00+00:00",
        "2026-05-20T01:00:00+00:00",
    ]
    assert [r["state"] for r in merged] == [2360.0, 0.2, 0.5]


class _ConfigWS:
    def __init__(self, tz: str) -> None:
        self._tz = tz
        self.calls: list[str] = []

    async def _call(self, msg_type, **kwargs):
        self.calls.append(msg_type)
        if msg_type == "get_config":
            return {"time_zone": self._tz}
        raise AssertionError(msg_type)


def test_get_timezone_reads_ha_config():
    mod = _load_migrate_module()
    ws = mod.HAWebSocket.__new__(mod.HAWebSocket)
    ws._call = _ConfigWS("Europe/London")._call  # type: ignore[attr-defined]
    tz = asyncio.run(mod.HAWebSocket.get_timezone(ws))
    assert str(tz) == "Europe/London"


def test_mean_pairs_present_and_shaped():
    mod = _load_migrate_module()
    suffixes = {gt for (gt, _ge, _desc) in mod.MEAN_PAIRS}
    assert any("pv_power" in s for s in suffixes)
    assert any("grid_power" in s or "import_power" in s for s in suffixes)
    assert any("battery_power" in s or "charge_power" in s for s in suffixes)
    assert all(len(t) == 3 for t in mod.MEAN_PAIRS)


def test_mean_metadata_is_mean_not_sum():
    mod = _load_migrate_module()
    meta = mod.mean_metadata("sensor.loft_givenergy_inverter_x_pv_power", "W")
    assert meta["has_mean"] is True
    assert meta["has_sum"] is False
    assert meta["statistic_id"] == "sensor.loft_givenergy_inverter_x_pv_power"


def test_find_implausible_hours_flags_over_ceiling():
    rows = [
        {"start": "2026-05-20T12:00:00+00:00", "sum": 100.0},
        {"start": "2026-05-20T13:00:00+00:00", "sum": 27496.0},  # +27396 jump
        {"start": "2026-05-20T14:00:00+00:00", "sum": 27497.0},
    ]
    flagged = _MOD.find_implausible_hours(rows, ceiling=50.0)
    assert [f["start"] for f in flagged] == ["2026-05-20T13:00:00+00:00"]


def test_find_duplicate_series_detects_identical():
    a = [{"start": "t1", "sum": 1.0}, {"start": "t2", "sum": 2.0}]
    b = [{"start": "t1", "sum": 1.0}, {"start": "t2", "sum": 2.0}]
    c = [{"start": "t1", "sum": 9.0}, {"start": "t2", "sum": 9.0}]
    dupes = _MOD.find_duplicate_series({"a": a, "b": b, "c": c})
    assert ("a", "b") in dupes or ("b", "a") in dupes
    assert all("c" not in pair for pair in dupes)


def test_classify_gaps_marks_contiguous_missing():
    rows = [
        {"start": "2026-05-20T12:00:00+00:00", "state": 1.0},
        # 13:00 and 14:00 missing
        {"start": "2026-05-20T15:00:00+00:00", "state": 1.0},
    ]
    gaps = _MOD.classify_gaps(rows, expected_step_minutes=60)
    assert gaps and gaps[0]["hours"] == 2


def test_find_fake_reset_shapes_detects_drop_then_spike():
    rows = [
        {"start": "t1", "state": 200.0},
        {"start": "t2", "state": 0.0},  # drop to ~0
        {"start": "t3", "state": 27300.0},  # huge positive
    ]
    shapes = _MOD.find_fake_reset_shapes(rows, ceiling=50.0)
    assert shapes and shapes[0]["start"] == "t3"


def test_format_validation_report_summarises_findings():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "implausible": [{"start": "t", "change": 27396.0}],
            "fake_resets": [],
            "gaps": [{"after": "a", "before": "b", "hours": 2}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[("a", "b")])
    assert "sensor.x" in text
    assert "27396" in text
    assert exit_code != 0  # substantive findings -> non-zero


# ---------------------------------------------------------------------------
# _repairable guard tests
# ---------------------------------------------------------------------------


def test_repairable_sum_entity_with_implausible_is_true():
    mod = _load_migrate_module()
    units_by_id = {"sensor.ge_pv_energy_today": "kWh"}
    implausible = [{"start": "2026-05-20T13:00:00+00:00", "change": 27396.0}]
    assert mod._repairable("sensor.ge_pv_energy_today", units_by_id, implausible) is True


def test_repairable_sum_entity_without_implausible_is_false():
    mod = _load_migrate_module()
    units_by_id = {"sensor.ge_pv_energy_today": "kWh"}
    assert mod._repairable("sensor.ge_pv_energy_today", units_by_id, []) is False


def test_repairable_mean_entity_excluded_even_with_implausible():
    """A mean entity (ge_id not in units_by_id) must never be queued for repair,
    even if find_implausible_hours returns findings for it."""
    mod = _load_migrate_module()
    units_by_id = {"sensor.ge_pv_energy_today": "kWh"}  # only the sum entity
    implausible = [{"start": "2026-05-20T13:00:00+00:00", "change": 99999.0}]
    # mean entity ge_id is NOT in units_by_id
    assert mod._repairable("sensor.ge_pv_power", units_by_id, implausible) is False


def test_repairable_mean_entity_excluded_when_units_by_id_empty():
    mod = _load_migrate_module()
    implausible = [{"start": "2026-05-20T13:00:00+00:00", "change": 99999.0}]
    assert mod._repairable("sensor.ge_pv_power", {}, implausible) is False
