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


def test_elapsed_hours():
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T09:00:00+00:00") == 1.0
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T13:00:00+00:00") == 5.0
    # never below 1 (guards the per-hour bound)
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T08:30:00+00:00") == 1.0


def test_gap_crosses_reset_daily():
    rc = _MOD.ResetClass
    # 20:00Z -> 06:00Z next day in BST: a local midnight (23:00Z) lies strictly inside
    assert _MOD._gap_crosses_reset(
        "2026-05-20T20:00:00+00:00", "2026-05-21T06:00:00+00:00", rc.DAILY, _LONDON
    )
    # same-day morning, no midnight between
    assert not _MOD._gap_crosses_reset(
        "2026-05-20T08:00:00+00:00", "2026-05-20T13:00:00+00:00", rc.DAILY, _LONDON
    )


def test_gap_crosses_reset_daily_endpoint_on_boundary_is_not_crossing():
    rc = _MOD.ResetClass
    # start exactly at local midnight (23:00Z BST): boundary is AT the endpoint,
    # not strictly inside -> a normal reset row, not a gap_undercount
    assert not _MOD._gap_crosses_reset(
        "2026-05-20T20:00:00+00:00", "2026-05-20T23:00:00+00:00", rc.DAILY, _LONDON
    )


def test_gap_crosses_reset_lifetime_never():
    assert not _MOD._gap_crosses_reset(
        "2026-05-20T08:00:00+00:00", "2026-06-01T00:00:00+00:00", _MOD.ResetClass.LIFETIME, _LONDON
    )


def test_gap_crosses_reset_annual_year_end():
    rc = _MOD.ResetClass
    assert _MOD._gap_crosses_reset(
        "2025-12-31T20:00:00+00:00", "2026-01-01T06:00:00+00:00", rc.ANNUAL, _LONDON
    )
    assert not _MOD._gap_crosses_reset(
        "2026-03-01T00:00:00+00:00", "2026-03-05T00:00:00+00:00", rc.ANNUAL, _LONDON
    )


def test_gap_crosses_reset_annual_endpoint_on_boundary_is_not_crossing():
    rc = _MOD.ResetClass
    # start exactly at Jan-1 00:00 local (GMT): boundary AT endpoint, not inside
    assert not _MOD._gap_crosses_reset(
        "2025-12-31T20:00:00+00:00", "2026-01-01T00:00:00+00:00", rc.ANNUAL, _LONDON
    )


def test_segment_coherent_monotonic_within_bound():
    held = [
        ("2026-05-20T12:00:00+00:00", 1000.0),
        ("2026-05-20T13:00:00+00:00", 1002.7),
        ("2026-05-20T14:00:00+00:00", 1005.1),
    ]  # +2.7, +2.4 over 1h each
    assert _MOD._segment_coherent(held, 25.0, _LONDON)


def test_segment_coherent_rejects_oscillation():
    held = [
        ("2026-05-20T12:00:00+00:00", 1000.0),
        ("2026-05-20T13:00:00+00:00", 1010.0),
        ("2026-05-20T14:00:00+00:00", 1001.0),
    ]  # internal negative delta
    assert not _MOD._segment_coherent(held, 25.0, _LONDON)


def test_segment_coherent_per_pair_elapsed_bound():
    # all hourly: a +60 internal delta over 1h exceeds 1*25 even though the
    # cumulative span (3h) would permit 75 — must be rejected.
    held = [
        ("2026-05-20T12:00:00+00:00", 1000.0),
        ("2026-05-20T13:00:00+00:00", 1002.0),
        ("2026-05-20T14:00:00+00:00", 1062.0),
    ]  # +60 over 1h
    assert not _MOD._segment_coherent(held, 25.0, _LONDON)


def test_segment_coherent_irregular_spacing_uses_each_gap():
    # 5h between the last pair allows a larger (but still <= 25*5) delta
    held = [
        ("2026-05-20T12:00:00+00:00", 1000.0),
        ("2026-05-20T13:00:00+00:00", 1002.0),
        ("2026-05-20T18:00:00+00:00", 1100.0),
    ]  # +98 over 5h <= 125
    assert _MOD._segment_coherent(held, 25.0, _LONDON)


def test_smear_gap_daily_boundaries_climb_linearly():
    # 100 -> +60 over 2026-05-20T12:00Z .. 2026-05-23T12:00Z (3 days)
    rows = _MOD._smear_gap(
        100.0, 60.0, "2026-05-20T12:00:00+00:00", "2026-05-23T12:00:00+00:00", _LONDON
    )
    # local midnights strictly inside: 05-21, 05-22, 05-23 (00:00 local == 23:00Z prev day in BST)
    assert len(rows) >= 2
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)  # monotonic climb
    assert all(100.0 < s < 160.0 for s in sums)  # strictly between start and end
    assert all(r["state"] == r["sum"] for r in rows)


def test_smear_gap_zero_or_negative_span_empty():
    assert (
        _MOD._smear_gap(
            10.0, 5.0, "2026-05-20T12:00:00+00:00", "2026-05-20T12:00:00+00:00", _LONDON
        )
        == []
    )


def _walk(rows, rc, ceiling, events=None):
    return _MOD.rebuild_sum_walk(rows, rc, ceiling, _LONDON, events=events)


def test_walk_accepts_genuine_climb():
    rows = [
        _row("2026-05-20T08:00:00+00:00", 100.0),
        _row("2026-05-20T09:00:00+00:00", 102.0),
        _row("2026-05-20T10:00:00+00:00", 105.0),
    ]
    assert _sums(_walk(rows, _MOD.ResetClass.LIFETIME, 50.0)) == [100.0, 102.0, 105.0]


def test_walk_transient_spike_held():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 200.0),
        _row("2026-05-20T13:00:00+00:00", 9999.0),  # 1-reading spike
        _row("2026-05-20T14:00:00+00:00", 203.0),
    ]
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 50.0)
    assert _sums(out) == [200.0, 200.0, 203.0]
    assert [r["state"] for r in out] == [200.0, 200.0, 203.0]  # held state = last-good


def test_walk_sustained_upward_rebase_recovers_and_books_internal():
    # The live failure: climb, an ~8000 artifact jump, then genuine +2.7/+2.4 climb.
    rows = [
        _row("2026-05-20T10:00:00+00:00", 1000.0),
        _row("2026-05-20T11:00:00+00:00", 1003.0),
        _row("2026-05-20T12:00:00+00:00", 9003.0),  # +8000 artifact (offset)
        _row("2026-05-20T13:00:00+00:00", 9005.7),  # +2.7 internal
        _row("2026-05-20T14:00:00+00:00", 9008.1),  # +2.4 internal
        _row("2026-05-20T15:00:00+00:00", 9010.0),
    ]  # +1.9 post-segment
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    sums = _sums(out)
    assert sums == sorted(sums)  # monotonic, NOT flat
    assert max(b - a for a, b in zip(sums, sums[1:])) < 25.0  # offset suppressed
    # genuine accumulation retained: 1003 -> ~1003+2.7+2.4+1.9 = 1010.0
    assert abs(sums[-1] - 1010.0) < 0.01
    assert events.get("rebaseline")  # a re-baseline was recorded


def test_walk_sustained_downward_rebase_recovers():
    rows = [
        _row("2026-05-20T10:00:00+00:00", 9000.0),
        _row("2026-05-20T11:00:00+00:00", 9002.0),
        _row("2026-05-20T12:00:00+00:00", 10.0),  # downward meter reset (offset)
        _row("2026-05-20T13:00:00+00:00", 12.0),  # +2 internal
        _row("2026-05-20T14:00:00+00:00", 14.0),
    ]  # +2 internal
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0)
    sums = _sums(out)
    assert sums == sorted(sums)  # still monotonic (offset suppressed)
    assert abs((sums[-1] - sums[1]) - 4.0) < 0.01  # only the +2+2 booked after the climb


def test_walk_three_corrupt_highs_then_recovery_not_a_segment():
    # corrupt highs that are NOT a coherent climb, then return to real lower value
    rows = [
        _row("2026-05-20T10:00:00+00:00", 1000.0),
        _row("2026-05-20T11:00:00+00:00", 9000.0),  # corrupt
        _row("2026-05-20T12:00:00+00:00", 200.0),  # corrupt (internal delta -8800)
        _row("2026-05-20T13:00:00+00:00", 9000.0),  # corrupt
        _row("2026-05-20T14:00:00+00:00", 1002.0),
    ]  # back to real
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0)
    # never re-baselined to a corrupt level; recovers against last-good 1000 -> 1002
    assert _sums(out) == [1000.0, 1000.0, 1000.0, 1000.0, 1002.0]


def test_walk_smears_lifetime_gap():
    # 12h gap with a +6 genuine delta (<= 25*12); LIFETIME -> smear daily
    rows = [_row("2026-05-20T12:00:00+00:00", 100.0), _row("2026-05-21T00:00:00+00:00", 106.0)]
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    assert _sums(out)[-1] == 106.0
    assert events.get("smear")
    # at least one synthesised intermediate row between the two
    assert len(out) > len(rows)


def test_walk_daily_midnight_gap_negative_delta_is_gap_undercount():
    # DAILY counter, gap crosses midnight, negative endpoint delta (8 -> 2)
    rows = [
        _row("2026-05-20T22:00:00+00:00", 8.0),  # 23:00 local
        _row("2026-05-21T02:00:00+00:00", 2.0),
    ]  # next day, after reset
    events = {}
    out = _walk(rows, _MOD.ResetClass.DAILY, 25.0, events=events)
    # carry flat across the gap (under-count), do NOT treat as rebase/segment
    assert _sums(out) == [8.0, 8.0]
    assert events.get("gap_undercount")
    assert not events.get("rebaseline")


def test_walk_unresolved_held_tail_recorded():
    # a short over-bound run at EOF that never confirms or reverts
    rows = [
        _row("2026-05-20T10:00:00+00:00", 100.0),
        _row("2026-05-20T11:00:00+00:00", 9000.0),
        _row("2026-05-20T12:00:00+00:00", 9002.0),
    ]  # only 2 held (< K=3), then EOF
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    assert events.get("unresolved")  # tail flagged
    assert _sums(out) == [100.0, 100.0, 100.0]  # emitted last-good (flat) but FLAGGED


def test_walk_carries_gap_rows_none_state():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 100.0),
        _row("2026-05-20T13:00:00+00:00", None),
        _row("2026-05-20T14:00:00+00:00", 101.0),
    ]
    assert _sums(_walk(rows, _MOD.ResetClass.LIFETIME, 50.0)) == [100.0, 100.0, 101.0]


def test_walk_none_row_between_held_keeps_output_sorted():
    # A None gap row arrives between buffered (held) rows; held rows are emitted
    # later when the segment confirms, so the raw append order is out of order.
    # The function contract is rows sorted ascending by start.
    rows = [
        _row("2026-05-20T10:00:00+00:00", 100.0),
        _row("2026-05-20T11:00:00+00:00", 9000.0),  # over-bound -> held
        _row("2026-05-20T12:00:00+00:00", 9002.0),  # held
        _row("2026-05-20T13:00:00+00:00", None),  # gap row emitted immediately
        _row("2026-05-20T14:00:00+00:00", 9004.0),  # 3rd held -> segment flush
    ]
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0)
    starts = [_MOD._to_utc(r["start"]) for r in out]
    assert starts == sorted(starts)  # non-decreasing


def test_walk_leading_gap_row_keeps_none_state():
    # A None row before any accepted data must not fabricate a 0.0 reading.
    rows = [
        _row("2026-05-20T12:00:00+00:00", None),
        _row("2026-05-20T13:00:00+00:00", 100.0),
    ]
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 50.0)
    assert out[0]["state"] is None


def test_walk_no_smear_event_when_no_rows_synthesised():
    # Transient spike, then recovery 2h later within the same local day (no
    # midnight crossed). _smear_gap returns [] -> no smear event should be
    # recorded, but the recovery delta is still booked.
    rows = [
        _row("2026-05-20T10:00:00+00:00", 100.0),
        _row("2026-05-20T11:00:00+00:00", 9999.0),  # transient spike -> held
        _row("2026-05-20T13:00:00+00:00", 103.0),  # recovery 2h later, +3 genuine
    ]
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    assert not events.get("smear")
    assert _sums(out)[-1] == 103.0  # delta still booked


def test_find_flat_line_spans_duration_from_timestamps():
    # 8 hourly rows 00:00..07:00 == 7 hours of flat, not 8
    rows = [{"start": f"2026-05-20T{h:02d}:00:00+00:00", "sum": 100.0} for h in range(8)]
    spans = _MOD.find_flat_line_spans(rows, min_hours=6)
    assert spans and abs(spans[0]["hours"] - 7.0) < 1e-9
    assert spans[0]["start"] == "2026-05-20T00:00:00+00:00"
    assert spans[0]["end"] == "2026-05-20T07:00:00+00:00"


def test_find_flat_line_spans_ignores_short_flat():
    # 00:00..03:00 == 3 hours < 6
    rows = [{"start": f"2026-05-20T{h:02d}:00:00+00:00", "sum": 100.0} for h in range(4)]
    assert _MOD.find_flat_line_spans(rows, min_hours=6) == []


def test_find_flat_line_spans_epsilon_equality():
    # sub-epsilon jitter counts as flat
    rows = [{"start": f"2026-05-20T{h:02d}:00:00+00:00", "sum": 100.0 + h * 1e-9} for h in range(8)]
    spans = _MOD.find_flat_line_spans(rows, min_hours=6)
    assert spans and abs(spans[0]["hours"] - 7.0) < 1e-9


def test_find_flat_line_spans_irregular_spacing():
    # a single 8h-apart pair with equal sum is an 8h flat span
    rows = [
        {"start": "2026-05-20T00:00:00+00:00", "sum": 100.0},
        {"start": "2026-05-20T08:00:00+00:00", "sum": 100.0},
    ]
    spans = _MOD.find_flat_line_spans(rows, min_hours=6)
    assert spans and abs(spans[0]["hours"] - 8.0) < 1e-9


def test_reset_aware_movement_counts_post_reset():
    rc = _MOD.ResetClass
    # DAILY: 2,5 then reset to 0,3 -> genuine movement = (5-2) + 3 = 6, not 3
    rows = [
        _row("2026-05-20T22:00:00+00:00", 2.0),
        _row("2026-05-20T22:30:00+00:00", 5.0),
        _row("2026-05-20T23:00:00+00:00", 0.0),  # local-midnight reset (BST)
        _row("2026-05-21T00:00:00+00:00", 3.0),
    ]
    assert abs(_MOD._reset_aware_movement(rows, rc.DAILY, _LONDON) - 6.0) < 1e-6


def test_reset_aware_movement_lifetime_is_positive_deltas():
    rc = _MOD.ResetClass
    rows = [
        _row("2026-05-20T10:00:00+00:00", 100.0),
        _row("2026-05-20T11:00:00+00:00", 103.0),
        _row("2026-05-20T12:00:00+00:00", 105.0),
    ]
    assert abs(_MOD._reset_aware_movement(rows, rc.LIFETIME, _LONDON) - 5.0) < 1e-6


def test_compare_source_movement_excludes_upward_offset_only():
    # source moved 100 genuine + an 8000 upward rebase offset; rebuilt has 100
    res = _MOD.compare_source_movement(
        source_movement=8100.0, rebuilt_movement=100.0, upward_offsets=8000.0, tol_pct=5.0
    )
    assert res["flagged"] is False and abs(res["expected"] - 100.0) < 1e-9


def test_compare_source_movement_downward_offset_not_subtracted():
    # a downward rebase: its offset is negative and was never in positive movement,
    # so upward_offsets is 0 -> expected stays 100, matches rebuilt 100
    res = _MOD.compare_source_movement(
        source_movement=100.0, rebuilt_movement=100.0, upward_offsets=0.0, tol_pct=5.0
    )
    assert res["flagged"] is False and res["expected"] == 100.0


def test_compare_source_movement_flags_real_divergence():
    res = _MOD.compare_source_movement(
        source_movement=100.0, rebuilt_movement=60.0, upward_offsets=0.0, tol_pct=5.0
    )
    assert res["flagged"] is True and abs(res["expected"] - 100.0) < 1e-9


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
    text, exit_code = mod.format_validation_report(findings, duplicates=[("a", "b")])
    assert "sensor.x" in text
    assert "27396" in text
    assert exit_code != 0  # substantive findings -> non-zero


def test_format_validation_report_header_distinguishes_mode():
    mod = _load_migrate_module()
    findings: dict = {}
    dry_text, _ = mod.format_validation_report(findings, duplicates=[], mode="dry-run")
    apply_text, _ = mod.format_validation_report(findings, duplicates=[], mode="candidates")
    applied_text, _ = mod.format_validation_report(findings, duplicates=[], mode="post-migration")
    assert "dry-run" in dry_text and "current series" in dry_text
    # Apply mode: not a dry-run, and it's the candidates about to be written.
    assert "candidates to write" in apply_text
    assert "dry-run" not in apply_text and "current series" not in apply_text
    assert "post-migration" in applied_text
    assert "dry-run" not in applied_text


def test_format_validation_report_accepted_findings_warn_not_block():
    """rebaseline / smear / gap_undercount print as ACCEPTED and never set a
    non-zero exit code on their own."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "rebaseline": [{"start": "t1", "offset": 5.0, "held": 3}],
            "smear": [{"start": "t2", "energy": 1.5, "hours": 4.0}],
            "gap_undercount": [{"start": "t3", "from": "t2"}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "sensor.x" in text
    assert "ACCEPTED" in text
    # Each accepted finding type renders a line.
    assert "rebaseline" in text
    assert "smear" in text
    assert "gap" in text and "undercount" in text.replace("-", "")
    # Accepted findings must NOT block.
    assert "BLOCKING" not in text
    assert exit_code == 0


def test_format_validation_report_flat_lines_block():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "flat_lines": [{"start": "t1", "end": "t2", "hours": 30.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "flat" in text.lower()
    assert "BLOCKING" in text
    assert exit_code != 0


def test_format_validation_report_unresolved_blocks():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "unresolved": [{"start": "t1", "count": 4}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "unresolved" in text.lower()
    assert "BLOCKING" in text
    assert exit_code != 0


def test_format_validation_report_flagged_source_comparison_blocks():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "source_comparison": {
                "expected": 100.0,
                "rebuilt": 50.0,
                "diff_pct": 50.0,
                "flagged": True,
            },
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "source" in text.lower()
    assert "BLOCKING" in text
    assert exit_code != 0


def test_format_validation_report_unflagged_source_comparison_silent():
    """An un-flagged source_comparison must neither block nor be reported as an
    issue."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "source_comparison": {
                "expected": 100.0,
                "rebuilt": 100.0,
                "diff_pct": 0.0,
                "flagged": False,
            },
            "ge_preservation": {
                "expected": 10.0,
                "rebuilt": 10.0,
                "diff_pct": 0.0,
                "flagged": False,
            },
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "BLOCKING" not in text
    assert exit_code == 0


def test_format_validation_report_flagged_ge_preservation_blocks():
    """The post-cutover GE-preservation divergence blocks when flagged."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "ge_preservation": {
                "expected": 100.0,
                "rebuilt": 40.0,
                "diff_pct": 60.0,
                "flagged": True,
            },
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "preservation" in text.lower() or "ge" in text.lower()
    assert "BLOCKING" in text
    assert exit_code != 0


def test_format_validation_report_mixed_accepted_and_blocking():
    """Accepted findings alongside a blocking one: both render, exit is non-zero,
    and the accepted ones still carry the ACCEPTED marker."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "rebaseline": [{"start": "t1", "offset": 5.0, "held": 2}],
            "flat_lines": [{"start": "t2", "end": "t3", "hours": 48.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[])
    assert "ACCEPTED" in text
    assert "BLOCKING" in text
    assert exit_code != 0


# ---------------------------------------------------------------------------
# Apply-mode (candidates / post-migration) exit-code semantics: the report's
# exit code must match the apply gate's `blocking` set exactly. Findings that
# the gate deliberately excludes (implausible / fake_resets / duplicates) are
# rendered ADVISORY and do NOT set the exit code; only the gate-blocking set
# (flat_lines / unresolved / flagged source_comparison / flagged
# ge_preservation) sets it. Dry-run keeps the stricter BLOCKING behaviour.
# ---------------------------------------------------------------------------


def test_format_validation_report_implausible_advisory_in_apply_mode():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "implausible": [{"start": "t1", "change": 27396.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[], mode="candidates")
    assert "ADVISORY" in text
    assert "BLOCKING" not in text
    assert exit_code == 0


def test_format_validation_report_fake_resets_advisory_in_apply_mode():
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "fake_resets": [{"start": "t1", "recovery": 500.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[], mode="candidates")
    assert "ADVISORY" in text
    assert "BLOCKING" not in text
    assert exit_code == 0


def test_format_validation_report_duplicates_advisory_in_apply_mode():
    mod = _load_migrate_module()
    text, exit_code = mod.format_validation_report({}, duplicates=[("a", "b")], mode="candidates")
    assert "ADVISORY" in text
    assert "BLOCKING" not in text
    assert exit_code == 0


def test_format_validation_report_blocking_wins_over_advisory_in_apply_mode():
    """A gate-blocking finding alongside an advisory one: exit is non-zero
    (blocking wins) and the implausible finding still renders advisory."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "implausible": [{"start": "t1", "change": 27396.0}],
            "flat_lines": [{"start": "t2", "end": "t3", "hours": 30.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[], mode="candidates")
    assert "ADVISORY" in text
    assert "BLOCKING" in text
    assert exit_code != 0


def test_format_validation_report_implausible_blocks_in_dry_run():
    """Dry-run preserves the stricter contract: implausible remains BLOCKING and
    exit-affecting, since there's no gate/repair to absorb the residue."""
    mod = _load_migrate_module()
    findings = {
        "sensor.x": {
            "implausible": [{"start": "t1", "change": 27396.0}],
        }
    }
    text, exit_code = mod.format_validation_report(findings, duplicates=[], mode="dry-run")
    assert "BLOCKING" in text
    assert "ADVISORY" not in text
    assert exit_code != 0


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


# ---------------------------------------------------------------------------
# _unexplained_flat_portions tests
# ---------------------------------------------------------------------------


def test_unexplained_flat_portions_short_covered_gap_leaves_residual():
    # A long flat (00:00..12:00 == 12h) containing one short covered gap
    # (03:00..05:00) still yields residual contiguous pieces — the 00:00..03:00
    # (3h) and 05:00..12:00 (7h) parts. The 7h part is >= the 6h threshold.
    span = {
        "start": "2026-05-20T00:00:00+00:00",
        "end": "2026-05-20T12:00:00+00:00",
        "hours": 12.0,
    }
    covered = [("2026-05-20T03:00:00+00:00", "2026-05-20T05:00:00+00:00")]
    residual = _MOD._unexplained_flat_portions(span, covered)
    hours = sorted(round(p["hours"], 6) for p in residual)
    assert hours == [3.0, 7.0]
    assert any(p["hours"] >= 6 for p in residual)  # 7h residual is blocking


def test_unexplained_flat_portions_fully_covered_yields_nothing():
    span = {
        "start": "2026-05-20T00:00:00+00:00",
        "end": "2026-05-20T08:00:00+00:00",
        "hours": 8.0,
    }
    # gap interval spans the whole flat (with margin) -> no residual
    covered = [("2026-05-19T23:00:00+00:00", "2026-05-20T09:00:00+00:00")]
    assert _MOD._unexplained_flat_portions(span, covered) == []


def test_unexplained_flat_portions_no_intervals_returns_whole_span():
    span = {
        "start": "2026-05-20T00:00:00+00:00",
        "end": "2026-05-20T07:00:00+00:00",
        "hours": 7.0,
    }
    residual = _MOD._unexplained_flat_portions(span, [])
    assert len(residual) == 1
    assert abs(residual[0]["hours"] - 7.0) < 1e-9
    assert residual[0]["start"] == "2026-05-20T00:00:00+00:00"
    assert residual[0]["end"] == "2026-05-20T07:00:00+00:00"


# ---------------------------------------------------------------------------
# run_validation candidate-based tests
# ---------------------------------------------------------------------------


class _RecordingWS:
    """Records get_statistics/clear/import calls. read_back configures the
    series get_statistics returns (keyed by id) for repair/verify paths."""

    def __init__(self, read_back: dict[str, list[dict]] | None = None) -> None:
        self.read_back = read_back or {}
        self.get_calls: list[list[str]] = []
        self.clear_calls: list[list[str]] = []
        self.import_calls: list[dict] = []

    async def get_statistics(self, ids, start, end=None, types=None):  # noqa: ANN001
        self.get_calls.append(list(ids))
        return {i: self.read_back.get(i, []) for i in ids}

    async def clear_statistics(self, ids):  # noqa: ANN001
        self.clear_calls.append(list(ids))

    async def import_statistics(self, metadata, stats):  # noqa: ANN001
        self.import_calls.append({"metadata": metadata, "stats": stats})


def _candidate(ge_id: str, rebuilt_rows: list[dict], **kw) -> object:
    r = _MOD.MigrationResult("desc", ge_id)
    r.status = "candidate"
    r.rebuilt_rows = rebuilt_rows
    r.events = kw.get("events", {})
    r.source_movement = kw.get("source_movement", 0.0)
    r.upward_offsets = kw.get("upward_offsets", 0.0)
    r.post_movement = kw.get("post_movement", 0.0)
    r.ge_post_movement = kw.get("ge_post_movement", 0.0)
    r.metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": ge_id,
        "unit_of_measurement": "kWh",
    }
    return r


def _flat_rows(start_hour: int, count: int, value: float) -> list[dict]:
    return [
        {
            "start": f"2026-05-20T{start_hour + h:02d}:00:00+00:00",
            "sum": value,
            "state": value,
        }
        for h in range(count)
    ]


_CUTOVER_DT = __import__("datetime").datetime(2026, 5, 20, tzinfo=__import__("datetime").UTC)


def test_run_validation_does_not_reread_ha_for_candidate():
    # A clean candidate validates without any get_statistics call.
    rows = [
        {"start": "2026-05-20T08:00:00+00:00", "sum": 100.0, "state": 100.0},
        {"start": "2026-05-20T09:00:00+00:00", "sum": 102.0, "state": 102.0},
        {"start": "2026-05-20T10:00:00+00:00", "sum": 105.0, "state": 105.0},
    ]
    # rows are pre-cutover (climb of +5 matches source_movement) -> no divergence.
    late_cutover = __import__("datetime").datetime(2026, 5, 21, tzinfo=__import__("datetime").UTC)
    r = _candidate("sensor.ge_x", rows, source_movement=5.0, post_movement=0.0)
    ws = _RecordingWS()
    exit_code, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=late_cutover,
            max_kwh=50.0,
        )
    )
    assert ws.get_calls == []  # candidate path never re-reads HA
    assert blocking is False


def test_run_validation_unexplained_flat_is_blocking():
    # A 12h flat span not covered by any gap_undercount -> blocking.
    rows = _flat_rows(0, 13, 100.0)  # 00:00..12:00 == 12h flat
    r = _candidate("sensor.ge_x", rows, source_movement=0.0)
    ws = _RecordingWS()
    exit_code, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=_CUTOVER_DT,
            max_kwh=50.0,
        )
    )
    assert blocking is True


def test_run_validation_flat_covered_by_gap_undercount_not_blocking():
    # The whole 12h flat is covered by a recorded gap_undercount -> warned, not blocking.
    rows = _flat_rows(0, 13, 100.0)  # 00:00..12:00
    events = {
        "gap_undercount": [
            {"from": "2026-05-19T23:00:00+00:00", "start": "2026-05-20T13:00:00+00:00"}
        ]
    }
    r = _candidate("sensor.ge_x", rows, events=events, source_movement=0.0)
    ws = _RecordingWS()
    exit_code, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=_CUTOVER_DT,
            max_kwh=50.0,
        )
    )
    assert blocking is False


def test_run_validation_long_flat_with_short_covered_gap_still_blocking():
    # 12h flat with a 2h covered gap inside leaves a 7h+ residual -> blocking.
    rows = _flat_rows(0, 13, 100.0)  # 00:00..12:00
    events = {
        "gap_undercount": [
            {"from": "2026-05-20T03:00:00+00:00", "start": "2026-05-20T05:00:00+00:00"}
        ]
    }
    r = _candidate("sensor.ge_x", rows, events=events, source_movement=0.0)
    ws = _RecordingWS()
    _exit, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=_CUTOVER_DT,
            max_kwh=50.0,
        )
    )
    assert blocking is True


def test_run_validation_unresolved_event_is_blocking():
    rows = [
        {"start": "2026-05-20T08:00:00+00:00", "sum": 100.0, "state": 100.0},
        {"start": "2026-05-20T09:00:00+00:00", "sum": 102.0, "state": 102.0},
    ]
    events = {"unresolved": [{"start": "2026-05-20T09:00:00+00:00", "count": 2}]}
    r = _candidate("sensor.ge_x", rows, events=events, source_movement=2.0)
    ws = _RecordingWS()
    _exit, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=_CUTOVER_DT,
            max_kwh=50.0,
        )
    )
    assert blocking is True


def test_run_validation_source_divergence_is_blocking():
    # Candidate moved 60 pre-cutover but source claims 100 (no upward offsets) -> flagged.
    rows = [
        {"start": "2026-05-20T08:00:00+00:00", "sum": 100.0, "state": 100.0},
        {"start": "2026-05-20T09:00:00+00:00", "sum": 160.0, "state": 160.0},
    ]
    # rows are post-cutover-ish; make pre-cutover window contain them by using a
    # late cutover so the candidate pre-movement = 60 vs source 100.
    late_cutover = __import__("datetime").datetime(2026, 5, 21, tzinfo=__import__("datetime").UTC)
    r = _candidate("sensor.ge_x", rows, source_movement=100.0, upward_offsets=0.0)
    ws = _RecordingWS()
    _exit, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=late_cutover,
            max_kwh=200.0,
        )
    )
    assert blocking is True


def test_run_validation_ge_preservation_divergence_is_blocking():
    # Post-cutover rebuilt movement diverges from the GE source movement -> blocking.
    rows = [
        {"start": "2026-05-21T08:00:00+00:00", "sum": 100.0, "state": 100.0},
        {"start": "2026-05-21T09:00:00+00:00", "sum": 110.0, "state": 110.0},
    ]
    r = _candidate(
        "sensor.ge_x",
        rows,
        source_movement=0.0,
        post_movement=10.0,
        ge_post_movement=40.0,  # GE source moved 40 post-cutover, rebuilt only 10
    )
    ws = _RecordingWS()
    _exit, blocking = asyncio.run(
        _MOD.run_validation(
            ws,
            [r],
            _LONDON,
            units_by_id={"sensor.ge_x": "kWh"},
            cutover=_CUTOVER_DT,
            max_kwh=50.0,
        )
    )
    assert blocking is True


# ---------------------------------------------------------------------------
# Two-phase apply: validate-all-then-write-all-or-abort (Task 9)
# ---------------------------------------------------------------------------


class _ApplyWS:
    """Fake WS driving run()'s apply path. Records clear/import/get calls and lets
    a test configure import failures (by ge_id) and the Phase-C read-back."""

    def __init__(
        self,
        read_back: dict[str, list[dict]] | None = None,
        import_fail_on: str | None = None,
    ) -> None:
        self.read_back = read_back or {}
        self.import_fail_on = import_fail_on
        self.clear_calls: list[list[str]] = []
        self.import_calls: list[dict] = []
        self.get_calls: list[list[str]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def get_timezone(self):
        return _LONDON

    async def list_statistic_ids(self, statistic_type: str = "sum"):
        # One inverter serial so run() doesn't bail on "no GivTCP data".
        return [{"statistic_id": "sensor.givtcp_ab1234c5_pv_energy_today_kwh"}]

    async def list_entity_registry(self):
        return []

    async def list_device_registry(self):
        return []

    async def clear_statistics(self, ids):  # noqa: ANN001
        self.clear_calls.append(list(ids))

    async def import_statistics(self, metadata, stats):  # noqa: ANN001
        ge_id = metadata["statistic_id"]
        if self.import_fail_on is not None and ge_id == self.import_fail_on:
            raise RuntimeError("simulated import failure")
        self.import_calls.append({"metadata": metadata, "stats": stats})

    async def get_statistics(self, ids, start, end=None, types=None):  # noqa: ANN001
        self.get_calls.append(list(ids))
        return {i: self.read_back.get(i, []) for i in ids}


def _apply_args() -> argparse.Namespace:
    return argparse.Namespace(
        ha_url="ws://test",
        token="t",
        cutover="2026-05-20",
        apply=True,
        include_charge_from_grid=False,
        trust_source_sums=False,
        max_kw=50.0,
    )


def _plan_entry(ge_id: str) -> tuple:
    # (givtcp_id, ge_id, desc, unit, warn, reset_class, resolved)
    return (f"sensor.givtcp_{ge_id}", ge_id, ge_id, "kWh", False, _MOD.ResetClass.LIFETIME, True)


def _patch_apply_path(
    monkeypatch: pytest.MonkeyPatch,
    ws: _ApplyWS,
    candidates_by_id: dict[str, object],
    plan_ids: list[str],
) -> None:
    """Wire run() to use the fake WS, a fixed plan, and pre-built candidates."""
    monkeypatch.setattr(_MOD, "HAWebSocket", lambda *a, **k: ws)
    monkeypatch.setattr(_MOD, "_ENTITY_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(_MOD, "_build_plan", lambda *a, **k: [_plan_entry(i) for i in plan_ids])
    # run() asks for interactive confirmation under --apply; auto-confirm it.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")

    async def fake_migrate(ws_arg, givtcp_id, ge_id, *a, **k):  # noqa: ANN001
        return candidates_by_id[ge_id]

    monkeypatch.setattr(_MOD, "migrate_entity", fake_migrate)


def test_phase_b_aborts_on_blocking_candidate(monkeypatch, capsys):
    # Two candidates; the second holds an unresolved event -> blocking. The gate
    # must refuse BEFORE Phase B: no clear/import calls at all, non-zero exit.
    clean = _candidate(
        "sensor.ge_a",
        [
            {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
            {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
        ],
        source_movement=2.0,
    )
    blocked = _candidate(
        "sensor.ge_b",
        [
            {"start": "2026-05-19T08:00:00+00:00", "sum": 20.0, "state": 20.0},
            {"start": "2026-05-19T09:00:00+00:00", "sum": 22.0, "state": 22.0},
        ],
        events={"unresolved": [{"start": "2026-05-19T09:00:00+00:00", "count": 2}]},
        source_movement=2.0,
    )
    ws = _ApplyWS()
    _patch_apply_path(
        monkeypatch,
        ws,
        {"sensor.ge_a": clean, "sensor.ge_b": blocked},
        ["sensor.ge_a", "sensor.ge_b"],
    )

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code != 0
    assert ws.clear_calls == []
    assert ws.import_calls == []
    assert "Refusing to --apply" in capsys.readouterr().err


def test_phase_b_mid_failure_reports_and_stops(monkeypatch, capsys):
    # import_statistics raises on the 2nd entity. Entity 1 fully written; entity 2
    # cleared (mid-write); entity 3 never touched. Loop stops; report names each
    # bucket and points to the backup. Non-zero exit.
    def clean(ge_id: str) -> object:
        return _candidate(
            ge_id,
            [
                {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
                {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
            ],
            source_movement=2.0,
        )

    cands = {i: clean(i) for i in ("sensor.ge_a", "sensor.ge_b", "sensor.ge_c")}
    ws = _ApplyWS(import_fail_on="sensor.ge_b")
    _patch_apply_path(monkeypatch, ws, cands, ["sensor.ge_a", "sensor.ge_b", "sensor.ge_c"])

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code != 0
    # ge_a written; ge_b cleared then import raised; ge_c never reached.
    assert ws.clear_calls == [["sensor.ge_a"], ["sensor.ge_b"]]
    assert [c["metadata"]["statistic_id"] for c in ws.import_calls] == ["sensor.ge_a"]
    err = capsys.readouterr().err
    assert "sensor.ge_a" in err  # fully written
    assert "sensor.ge_b" in err  # mid-write
    assert "sensor.ge_c" in err  # not touched
    assert "backup" in err.lower()


def test_phase_c_detects_stored_mismatch(monkeypatch, capsys):
    # (a) altered sum on read-back -> verify fails.
    rows = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
        {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
    ]
    cand = _candidate("sensor.ge_a", rows, source_movement=2.0)
    altered = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0},
        {"start": "2026-05-19T09:00:00+00:00", "sum": 99.0},  # sum differs
    ]
    ws = _ApplyWS(read_back={"sensor.ge_a": altered})
    _patch_apply_path(monkeypatch, ws, {"sensor.ge_a": cand}, ["sensor.ge_a"])

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code != 0
    assert cand.status != "migrated"
    err = capsys.readouterr().err
    assert "verification FAILED" in err
    assert "backup" in err.lower()


def test_phase_c_detects_shifted_timestamps(monkeypatch, capsys):
    # (b) SAME sums but shifted timestamps -> verify must still fail (compares start).
    rows = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
        {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
    ]
    cand = _candidate("sensor.ge_a", rows, source_movement=2.0)
    shifted = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0},
        {"start": "2026-05-19T10:00:00+00:00", "sum": 12.0},  # same sum, later hour
    ]
    ws = _ApplyWS(read_back={"sensor.ge_a": shifted})
    _patch_apply_path(monkeypatch, ws, {"sensor.ge_a": cand}, ["sensor.ge_a"])

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code != 0
    assert cand.status != "migrated"
    assert "verification FAILED" in capsys.readouterr().err


def test_phase_c_passes_on_matching_read_back(monkeypatch, capsys):
    # Happy path: read-back matches the candidate -> migrated, zero exit.
    rows = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
        {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
    ]
    cand = _candidate("sensor.ge_a", rows, source_movement=2.0)
    ws = _ApplyWS(read_back={"sensor.ge_a": [dict(r) for r in rows]})
    _patch_apply_path(monkeypatch, ws, {"sensor.ge_a": cand}, ["sensor.ge_a"])

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code == 0
    assert cand.status == "migrated"
    assert ws.clear_calls == [["sensor.ge_a"]]
    assert len(ws.import_calls) == 1


def test_phase_c_read_failure_aborts(monkeypatch, capsys):
    # get_statistics raises during Phase C -> the distinct "FAILED to re-read"
    # message fires, status stays un-migrated, non-zero exit.
    rows = [
        {"start": "2026-05-19T08:00:00+00:00", "sum": 10.0, "state": 10.0},
        {"start": "2026-05-19T09:00:00+00:00", "sum": 12.0, "state": 12.0},
    ]
    cand = _candidate("sensor.ge_a", rows, source_movement=2.0)
    ws = _ApplyWS(read_back={"sensor.ge_a": [dict(r) for r in rows]})

    async def boom(ids, start, end=None, types=None):  # noqa: ANN001
        raise RuntimeError("simulated read failure")

    ws.get_statistics = boom
    _patch_apply_path(monkeypatch, ws, {"sensor.ge_a": cand}, ["sensor.ge_a"])

    code = asyncio.run(_MOD.run(_apply_args()))

    assert code != 0
    assert cand.status != "migrated"
    err = capsys.readouterr().err
    assert "FAILED to re-read" in err
    assert "backup" in err.lower()


# ---------------------------------------------------------------------------
# End-to-end acceptance: the live failure through the REAL build → gate → write
# (Task 12). These differ from the Phase-B/C tests above by NOT patching
# migrate_entity — run() builds each candidate itself via the real migrate_entity
# from raw series the fake WS serves, so the whole candidate-build path (merged
# states, adaptive/effective ceiling, rebuild_sum_walk, reset-aware movement) is
# exercised end-to-end, then the real run_validation gate and Phase B/C.
# ---------------------------------------------------------------------------


# The live-failure source state timeline (cf.
# test_walk_sustained_upward_rebase_recovers_and_books_internal): genuine climb,
# an ~8000 artifact jump, then a coherent internal climb that re-baselines.
_LIVE_FAILURE_SOURCE = [
    _row("2026-05-20T10:00:00+00:00", 1000.0),
    _row("2026-05-20T11:00:00+00:00", 1003.0),
    _row("2026-05-20T12:00:00+00:00", 9003.0),  # +8000 artifact (offset)
    _row("2026-05-20T13:00:00+00:00", 9005.7),  # +2.7 internal
    _row("2026-05-20T14:00:00+00:00", 9008.1),  # +2.4 internal
    _row("2026-05-20T15:00:00+00:00", 9010.0),  # +1.9 post-segment
]


def _e2e_plan_entry(ge_id: str, reset_class) -> tuple:
    # As _plan_entry, but the GivTCP id matches the fake WS read-back key and the
    # reset class is explicit. resolved=True so --apply doesn't bail ge_not_found.
    return (f"sensor.givtcp_{ge_id}", ge_id, ge_id, "kWh", False, reset_class, True)


# Captures the MigrationResult objects the REAL migrate_entity returns during a
# run() (run() does not otherwise expose them), so tests can assert on the built
# candidate's status/events. Reset by _patch_real_build_path.
_LAST_RESULTS: list = []


def _patch_real_build_path(
    monkeypatch: pytest.MonkeyPatch,
    ws: _ApplyWS,
    plan: list[tuple],
) -> None:
    """Wire run() to the fake WS and a fixed plan but leave migrate_entity REAL,
    so the candidate is built from the series the WS serves (not injected). A thin
    recording shim around the real migrate_entity captures each result."""
    monkeypatch.setattr(_MOD, "HAWebSocket", lambda *a, **k: ws)
    monkeypatch.setattr(_MOD, "_ENTITY_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(_MOD, "_build_plan", lambda *a, **k: plan)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")

    _LAST_RESULTS.clear()
    real_migrate_entity = _MOD.migrate_entity

    async def recording_migrate_entity(*a, **k):  # noqa: ANN002, ANN003
        r = await real_migrate_entity(*a, **k)
        _LAST_RESULTS.append(r)
        return r

    monkeypatch.setattr(_MOD, "migrate_entity", recording_migrate_entity)


def _feed_back_on_import(ws: _ApplyWS) -> None:
    """Make Phase C see exactly what Phase B wrote: each import populates the
    read-back for that id with the imported rows (normalised by get_statistics)."""
    original = ws.import_statistics

    async def record_then_feed_back(metadata, stats):  # noqa: ANN001
        await original(metadata, stats)
        ws.read_back[metadata["statistic_id"]] = [dict(s) for s in stats]

    ws.import_statistics = record_then_feed_back


def _e2e_args() -> argparse.Namespace:
    # cutover 2026-05-21: the whole 2026-05-20 source series is pre-cutover, so
    # build_merged_states keeps it all and there is no post-cutover GE window.
    a = _apply_args()
    a.cutover = "2026-05-21"
    return a


def test_e2e_live_failure_rebuild_migrates_and_verifies(monkeypatch, capsys):
    """The live failure end-to-end through the candidate build, the gate, and a
    fake WS that reads back what it wrote:

      - the rebuilt candidate is monotonic and NOT flat;
      - its pre-cutover movement reconciles with cleaned source movement
        (compare_source_movement: source 8010 − 8000 upward offset == 10);
      - validation is non-blocking (a recorded rebaseline, no unexplained flat);
      - Phase C verify_written passes → status "migrated", exit 0.
    """
    ge_id = "sensor.givenergy_inverter_ab1234c5_pv_energy_total"  # LIFETIME suffix
    ws = _ApplyWS(read_back={f"sensor.givtcp_{ge_id}": _LIVE_FAILURE_SOURCE, ge_id: []})
    _feed_back_on_import(ws)
    _patch_real_build_path(monkeypatch, ws, [_e2e_plan_entry(ge_id, _MOD.ResetClass.LIFETIME)])

    code = asyncio.run(_MOD.run(_e2e_args()))

    # Exactly one candidate was built, written, and verified.
    assert ws.clear_calls == [[ge_id]]
    imports = [c for c in ws.import_calls if c["metadata"]["statistic_id"] == ge_id]
    assert len(imports) == 1
    written = imports[0]["stats"]
    sums = [r["sum"] for r in written]
    # Candidate is monotonic, the +8000 offset is suppressed, NOT flat-lined.
    assert sums == sorted(sums)
    assert max(b - a for a, b in zip(sums, sums[1:])) < 25.0
    assert sums[0] != sums[-1]  # genuine accumulation retained, not held flat
    assert abs(sums[-1] - 1010.0) < 0.01  # 1003 + 2.7 + 2.4 + 1.9
    # Validation reported the rebaseline as ACCEPTED and did not block; the real
    # gate let the write proceed and Phase C confirmed the stored series.
    out = capsys.readouterr().out
    assert "ACCEPTED" in out and "rebaseline" in out
    assert "BLOCKING" not in out
    # The migrate_entity build path produced the candidate (not injected):
    # status migrated, clean zero exit (no advisory implausible on the rebuilt).
    cand = next(r for r in _LAST_RESULTS if r.ge_id == ge_id)
    assert cand.status == "migrated"
    assert code == 0


def test_e2e_live_failure_phase_c_fails_on_altered_read_back(monkeypatch, capsys):
    """Same build, but the recorder hands back a mutated series at Phase C.
    verify_written must fail loudly: non-zero exit, status NOT migrated, and the
    failure message points at the backup."""
    ge_id = "sensor.givenergy_inverter_ab1234c5_pv_energy_total"
    ws = _ApplyWS(read_back={f"sensor.givtcp_{ge_id}": _LIVE_FAILURE_SOURCE, ge_id: []})

    original = ws.import_statistics

    async def import_then_corrupt_read_back(metadata, stats):  # noqa: ANN001
        await original(metadata, stats)
        altered = [dict(s) for s in stats]
        altered[-1] = {**altered[-1], "sum": altered[-1]["sum"] + 999.0}  # tamper
        ws.read_back[metadata["statistic_id"]] = altered

    ws.import_statistics = import_then_corrupt_read_back
    _patch_real_build_path(monkeypatch, ws, [_e2e_plan_entry(ge_id, _MOD.ResetClass.LIFETIME)])

    code = asyncio.run(_MOD.run(_e2e_args()))

    assert code != 0
    cand = next(r for r in _LAST_RESULTS if r.ge_id == ge_id)
    assert cand.status != "migrated"
    err = capsys.readouterr().err
    assert "verification FAILED" in err
    assert "backup" in err.lower()


def test_e2e_daily_day_crossing_gap_is_gap_undercount_and_migrates(monkeypatch, capsys):
    """A DAILY counter whose source crosses local midnight with a post-reset drop
    yields an ACCEPTED gap_undercount (not a rebase/segment): the candidate carries
    flat across the gap, validation does not block, and it migrates."""
    ge_id = "sensor.givenergy_inverter_ab1234c5_pv_energy_today"  # DAILY suffix
    source = [
        _row("2026-05-20T22:00:00+00:00", 8.0),  # 23:00 local (BST)
        _row("2026-05-21T02:00:00+00:00", 2.0),  # next local day, after reset
    ]
    # cutover after both rows so they're pre-cutover and there's no GE post window.
    ws = _ApplyWS(read_back={f"sensor.givtcp_{ge_id}": source, ge_id: []})
    _feed_back_on_import(ws)
    _patch_real_build_path(monkeypatch, ws, [_e2e_plan_entry(ge_id, _MOD.ResetClass.DAILY)])
    args = _apply_args()
    args.cutover = "2026-05-22"

    code = asyncio.run(_MOD.run(args))

    out = capsys.readouterr().out
    assert "gap-undercount" in out  # ACCEPTED line rendered
    assert "BLOCKING" not in out
    cand = next(r for r in _LAST_RESULTS if r.ge_id == ge_id)
    assert cand.status == "migrated"
    assert (cand.events or {}).get("gap_undercount")
    assert not (cand.events or {}).get("rebaseline")
    # Carried flat across the gap: both stored sums equal the last-good 8.0.
    written = next(c for c in ws.import_calls if c["metadata"]["statistic_id"] == ge_id)
    assert [r["sum"] for r in written["stats"]] == [8.0, 8.0]
    assert code == 0


def test_e2e_unexplained_flat_candidate_blocks_gate_no_writes(monkeypatch, capsys):
    """An unexplained flat candidate built by the real path is blocking: the apply
    gate refuses BEFORE Phase B — no clear/import of any kind, non-zero exit."""
    ge_id = "sensor.givenergy_inverter_ab1234c5_grid_import_total"  # LIFETIME
    # Oscillating corrupt highs that never form a coherent climbing segment (so the
    # walk never re-baselines): the held buffer is never confirmed as a real segment,
    # so the walk emits an `unresolved` held run — a blocking finding that makes the
    # gate refuse before any write.
    source = [_row("2026-05-20T00:00:00+00:00", 100.0)]
    source += [
        _row(f"2026-05-20T{h:02d}:00:00+00:00", 9000.0 if h % 2 else 200.0) for h in range(1, 9)
    ]
    source.append(_row("2026-05-20T09:00:00+00:00", 102.0))  # back to reality (no tail)
    ws = _ApplyWS(read_back={f"sensor.givtcp_{ge_id}": source, ge_id: []})
    _feed_back_on_import(ws)
    _patch_real_build_path(monkeypatch, ws, [_e2e_plan_entry(ge_id, _MOD.ResetClass.LIFETIME)])

    code = asyncio.run(_MOD.run(_e2e_args()))

    assert code != 0
    assert ws.clear_calls == []
    assert ws.import_calls == []
    err = capsys.readouterr().err
    assert "Refusing to --apply" in err


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
