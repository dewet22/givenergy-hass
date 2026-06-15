from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

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


def test_adaptive_ceiling_no_positive_deltas_returns_none():
    assert _MOD.adaptive_ceiling([0.0, 0.0, None]) is None


def test_adaptive_ceiling_below_min_samples_returns_none():
    # Sparse data: too few positive deltas. Returning a value here would let the
    # p99 estimate bless an order-of-magnitude spike.
    assert _MOD.adaptive_ceiling([1.0, 9900.0]) is None


def test_percentile_floor():
    vals = [1.0, 2.0, 3.0, 4.0]
    assert _MOD._percentile(vals, 50) == 2.0
    assert _MOD._percentile(vals, 0) == 1.0
    assert _MOD._percentile(vals, 100) == 4.0


def test_adaptive_ceiling_small_n_single_outlier_rejected():
    # 23 genuine deltas (<=6) + 1 huge outlier = 24 positive (the min-samples floor).
    # Floor-index p99 must land on a genuine value, so the outlier is rejected.
    genuine = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] + [3.0] * 17
    ceiling = _MOD.adaptive_ceiling(genuine + [27000.0])
    assert ceiling is not None
    assert ceiling < 27000.0  # outlier rejected, not blessed
    assert ceiling >= 6.0  # genuine peak still passes


def test_apply_requires_cap_logic():
    assert _MOD._apply_requires_cap(True, None) is True
    assert _MOD._apply_requires_cap(True, 6.0) is False
    assert _MOD._apply_requires_cap(False, None) is False


def test_adaptive_ceiling_tolerates_diurnal_peaks_rejects_fakes():
    """Realistic diurnal distribution: many overnight lows plus genuine peaks,
    plus a couple of huge fakes. Genuine peaks (<=6 kWh/h) must pass; the
    order-of-magnitude fakes (27k) must be rejected."""
    diurnal = ([0.1, 0.2, 0.3, 0.5, 0.7] * 40) + ([3.0, 4.0, 5.0, 6.0] * 20) + [27396.1, 29724.7]
    ceiling = _MOD.adaptive_ceiling(diurnal)
    assert ceiling is not None
    assert ceiling >= 6.0  # genuine midday/evening peaks (<=6) are NOT flagged
    assert ceiling < 50.0  # but the order-of-magnitude fakes are
    assert 27396.1 > ceiling and 6.0 <= ceiling


def test_effective_ceiling_both_none_is_none():
    assert _MOD.effective_ceiling(None, None) is None


def test_effective_ceiling_adaptive_none_uses_cap():
    assert _MOD.effective_ceiling(None, 7.0) == 7.0


def test_effective_ceiling_no_cap_uses_adaptive():
    assert _MOD.effective_ceiling(42.0, None) == 42.0


def test_effective_ceiling_cap_takes_precedence():
    # The user-declared cap is authoritative — used directly, even when the
    # adaptive estimate is lower (which would otherwise flatten genuine peaks).
    assert _MOD.effective_ceiling(42.0, 7.0) == 7.0
    assert _MOD.effective_ceiling(7.0, 42.0) == 42.0


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


def test_rebuild_walk_holds_state_not_just_sum_on_fake_zero():
    """The rejected row's OUTPUT state must be held to last-good (200), not the
    corrupt 0 it carried — otherwise the imported state timeline stays corrupt
    and post-migration state-based checks keep flagging it."""
    rows = [
        _row("2026-05-20T12:00:00+00:00", 200.0),
        _row("2026-05-20T13:00:00+00:00", 0.0),  # fake zero-read -> held
        _row("2026-05-20T14:00:00+00:00", 203.0),  # recovery
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert [r["state"] for r in out] == [200.0, 200.0, 203.0]
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


def test_dedup_series_excludes_mean_entities():
    """Two distinct mean series (sum=None, absent from units_by_id) sharing the
    same start timestamps must NOT be eligible for duplicate detection, while two
    truly-identical sum series (present in units_by_id) must remain eligible."""
    # Mean series: identical (start, None) key tuples but genuinely different data.
    mean_a = [
        {"start": "t1", "sum": None, "state": 100.0},
        {"start": "t2", "sum": None, "state": 200.0},
    ]
    mean_b = [
        {"start": "t1", "sum": None, "state": 50.0},
        {"start": "t2", "sum": None, "state": 25.0},
    ]
    # Sum series: byte-identical (start, sum) tuples — genuine duplicates.
    sum_a = [{"start": "t1", "sum": 1.0}, {"start": "t2", "sum": 2.0}]
    sum_b = [{"start": "t1", "sum": 1.0}, {"start": "t2", "sum": 2.0}]
    series_by_id = {
        "sensor.ge_pv_power": mean_a,
        "sensor.ge_soc": mean_b,
        "sensor.ge_pv_energy_today": sum_a,
        "sensor.ge_pv_energy_today_dup": sum_b,
    }
    units_by_id = {
        "sensor.ge_pv_energy_today": "kWh",
        "sensor.ge_pv_energy_today_dup": "kWh",
    }
    deduped = _MOD._dedup_series(series_by_id, units_by_id)
    # Mean entities dropped entirely from the dedup candidate set.
    assert set(deduped) == {"sensor.ge_pv_energy_today", "sensor.ge_pv_energy_today_dup"}
    dupes = _MOD.find_duplicate_series(deduped)
    # The two identical sum series ARE flagged …
    assert ("sensor.ge_pv_energy_today", "sensor.ge_pv_energy_today_dup") in dupes or (
        "sensor.ge_pv_energy_today_dup",
        "sensor.ge_pv_energy_today",
    ) in dupes
    # … and the two distinct mean series are NOT.
    assert all("sensor.ge_pv_power" not in pair and "sensor.ge_soc" not in pair for pair in dupes)


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


def test_find_fake_reset_shapes_ignores_negative_baseline():
    """A negative baseline (a < 0) must not invert the `b <= a * 0.05` test and
    spuriously flag a normal climb as a fake reset."""
    # With a negative baseline, `b <= a * 0.05` inverts: b=-10 <= -100*0.05=-5
    # is True, so an unguarded heuristic would falsely flag the t2->t3 climb.
    rows = [
        {"start": "t1", "state": -100.0},  # negative baseline
        {"start": "t2", "state": -10.0},
        {"start": "t3", "state": 30000.0},  # huge positive
    ]
    shapes = _MOD.find_fake_reset_shapes(rows, ceiling=50.0)
    assert shapes == []


def test_format_validation_report_summarises_findings():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "implausible": [{"start": "t", "change": 27396.0}],
            "fake_resets": [],
            "gaps": [{"after": "a", "before": "b", "hours": 2}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[("a", "b")], applied=True)
    assert "sensor.x" in text
    assert "27396" in text
    assert exit_code != 0  # substantive findings -> non-zero


def test_format_validation_report_header_distinguishes_mode():
    mod = _load_migrate_module()
    findings: dict = {}
    dry_text, _ = mod.format_validation_report(findings, duplicates=[], applied=False)
    applied_text, _ = mod.format_validation_report(findings, duplicates=[], applied=True)
    assert "dry-run" in dry_text and "current series" in dry_text
    assert "post-migration" in applied_text
    assert "dry-run" not in applied_text


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


# ---------------------------------------------------------------------------
# _repair_reset_class tests
# ---------------------------------------------------------------------------


def test_repair_reset_class_prefers_plan_over_suffix_inference():
    """A user-renamed entity whose ge_id no longer ends in _today is still
    classified as DAILY when the migration plan carries the authoritative class.

    This is the crux of the extraction: bare classify_entity on the renamed id
    would return LIFETIME, but _repair_reset_class returns the plan value.
    """
    mod = _load_migrate_module()
    rc = mod.ResetClass
    renamed_id = "sensor.loft_givenergy_inverter_x_solar_daily"
    reset_classes = {renamed_id: rc.DAILY}
    # Confirm the fallback alone would misclassify.
    assert mod.classify_entity(renamed_id) is rc.LIFETIME
    # The helper returns the plan value.
    assert mod._repair_reset_class(renamed_id, reset_classes) is rc.DAILY


def test_repair_reset_class_falls_back_to_suffix_when_absent():
    """An entity absent from the plan (or plan is None) falls back to classify_entity."""
    mod = _load_migrate_module()
    rc = mod.ResetClass
    assert mod._repair_reset_class("sensor.ge_pv_energy_today", {}) is rc.DAILY
    assert mod._repair_reset_class("sensor.ge_grid_import_total", None) is rc.LIFETIME


def test_acceptance_rebuild_heals_documented_corruption():
    """Reproduce the LTS-report shapes and assert rebuild + validation handle them.

    Shapes covered (from .remember/lts-corruption-report-2026-06-12.md):
      - genuine steady climb on a lifetime counter
      - an off-midnight zero-read + recovery (held, not double-counted)
      - a +27,396 kWh fake-reset spike (rejected)
    """
    rc = _MOD.ResetClass
    rows = [
        {"start": "2026-06-07T13:00:00+00:00", "state": 1000.0},
        {"start": "2026-06-07T14:00:00+00:00", "state": 1003.0},
        {"start": "2026-06-07T15:00:00+00:00", "state": 0.0},  # zero-read
        {"start": "2026-06-07T16:00:00+00:00", "state": 27396.1},  # fake spike/recovery
        {"start": "2026-06-07T17:00:00+00:00", "state": 1006.0},  # back to reality
    ]
    ceiling = _MOD.adaptive_ceiling([3.0, 2.0, 4.0, 3.0, 2.5] * 20)
    out = _MOD.rebuild_sum_walk(rows, rc.LIFETIME, ceiling, _LONDON)
    sums = [r["sum"] for r in out]
    # Monotonic non-decreasing, and no +50 jump anywhere.
    assert sums == sorted(sums)
    assert max(b - a for a, b in zip(sums, sums[1:])) < 50.0
    # Final sum reflects only genuine accumulation (1000 -> ~1006), not the spike.
    assert sums[-1] < 1010.0
    # Validation on the rebuilt series finds nothing implausible.
    assert _MOD.find_implausible_hours(out, ceiling) == []
    # The STATE timeline must also be clean: rejected rows hold last-good state,
    # not the corrupt 0/spike, so find_fake_reset_shapes (which reads state) has
    # nothing to flag and --repair-residue would find no residue.
    assert _MOD.find_fake_reset_shapes(out, ceiling) == []
    out_states = [r["state"] for r in out]
    assert 0.0 not in out_states  # the zero-read was overwritten with last-good
    assert 27396.1 not in out_states  # the fake spike was overwritten too


def test_positive_float_accepts_positive():
    assert _MOD._positive_float("6") == 6.0
    assert _MOD._positive_float("0.5") == 0.5


def test_positive_float_rejects_zero_and_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        _MOD._positive_float("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _MOD._positive_float("-3")


def test_positive_float_rejects_non_finite():
    # nan/inf pass an `f <= 0` check but would corrupt (nan) or unguard (inf) the
    # rebuild, so they must be rejected too.
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(argparse.ArgumentTypeError):
            _MOD._positive_float(value)
