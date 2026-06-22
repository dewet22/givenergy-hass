# GivTCP rebuild sustained-shift recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix `rebuild_sum_walk` so a sustained shift in the source no longer flat-lines the rebuilt sum (#172), with reset-aware gap handling, a two-phase whole-run apply gate, and validation that catches under-counting — all proven against the live failure.

**Architecture:** Rebuild becomes a buffered-segment walk: classify reset-crossing gaps first, accept time-scaled deltas (smearing non-reset gaps), and buffer over-bound/off-boundary readings until they prove transient (flush last-good) or a coherent monotonic segment (re-baseline: suppress the one-time offset, book internal deltas, emit normalized continuous state). Apply becomes two-phase: build+validate every candidate in memory, then write all-or-abort; backup is the recovery net.

**Tech Stack:** Python 3.14, stdlib (`zoneinfo`, `datetime`), pytest. Spec: `docs/superpowers/specs/2026-06-15-migrate-rebuild-rebaseline-design.md`.

---

## File structure

- **Modify** `scripts/migrate_from_givtcp.py`:
  - New pure helpers: `_elapsed_hours`, `_gap_crosses_reset`, `_segment_coherent`, `_smear_gap`.
  - Rewrite `rebuild_sum_walk` (buffered segments + event out-param).
  - New validation: `find_flat_line_spans`, `compare_source_movement`.
  - `MigrationResult` gains candidate/source/event fields.
  - `migrate_entity` populates the candidate (no longer writes).
  - `run`: two-phase apply (build+validate all → write all-or-abort).
  - `run_validation` / `format_validation_report`: candidate-based, new findings, accepted-vs-blocking.
- **Modify** `tests/test_migrate_stats_repair.py` — pure-helper tests, the walk fixture suite, validation tests, two-phase tests.

Existing helpers reused: `_to_utc`, `_as_iso`, `_is_reset_boundary`, `ResetClass`, `classify_entity`, `effective_ceiling`, `adaptive_ceiling`. Test loader: `_load_migrate_module()` / `_MOD`, `_LONDON`, `_row`, `_sums`.

Module constants to add near `_CEILING_*`:
```python
_REBASELINE_HOLDS = 3          # consecutive coherent holds before re-baselining
_FLAT_LINE_MIN_HOURS = 6       # min span for a flat-line finding
_MOVEMENT_TOLERANCE_PCT = 5.0  # source-vs-rebuilt movement divergence tolerance
```

---

## Task 1: `_elapsed_hours` helper

**Files:** Modify `scripts/migrate_from_givtcp.py` (pure-helpers region); Test `tests/test_migrate_stats_repair.py`.

- [ ] **Step 1: Failing test**
```python
def test_elapsed_hours():
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T09:00:00+00:00") == 1.0
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T13:00:00+00:00") == 5.0
    # never below 1 (guards the per-hour bound)
    assert _MOD._elapsed_hours("2026-05-20T08:00:00+00:00", "2026-05-20T08:30:00+00:00") == 1.0
```
- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_migrate_stats_repair.py -k elapsed_hours -v`
- [ ] **Step 3: Implement** (pure-helpers region)
```python
def _elapsed_hours(prev_start: str, start: str) -> float:
    """Whole-ish hours between two ISO timestamps, floored at 1 (so the per-hour
    bound applies to adjacent readings and scales up across a gap)."""
    delta = (_to_utc(start) - _to_utc(prev_start)).total_seconds() / 3600.0
    return max(1.0, delta)
```
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(migrate): _elapsed_hours helper (#172)"`

---

## Task 2: `_gap_crosses_reset` helper

Detects whether a DAILY/ANNUAL reset boundary lies strictly inside `(prev_start, start)` in local time.

**Files:** Modify script; Test.

- [ ] **Step 1: Failing test** (note the strict-boundary cases — a reading exactly on the boundary is a normal reset row, NOT a crossed gap):
```python
def test_gap_crosses_reset_daily():
    rc = _MOD.ResetClass
    # 20:00Z -> 06:00Z next day in BST: a local midnight (23:00Z) lies strictly inside
    assert _MOD._gap_crosses_reset("2026-05-20T20:00:00+00:00", "2026-05-21T06:00:00+00:00", rc.DAILY, _LONDON)
    # same-day morning, no midnight between
    assert not _MOD._gap_crosses_reset("2026-05-20T08:00:00+00:00", "2026-05-20T13:00:00+00:00", rc.DAILY, _LONDON)

def test_gap_crosses_reset_daily_endpoint_on_boundary_is_not_crossing():
    rc = _MOD.ResetClass
    # start exactly at local midnight (23:00Z BST): boundary is AT the endpoint,
    # not strictly inside -> a normal reset row, not a gap_undercount
    assert not _MOD._gap_crosses_reset("2026-05-20T20:00:00+00:00", "2026-05-20T23:00:00+00:00", rc.DAILY, _LONDON)

def test_gap_crosses_reset_lifetime_never():
    assert not _MOD._gap_crosses_reset("2026-05-20T08:00:00+00:00", "2026-06-01T00:00:00+00:00", _MOD.ResetClass.LIFETIME, _LONDON)

def test_gap_crosses_reset_annual_year_end():
    rc = _MOD.ResetClass
    assert _MOD._gap_crosses_reset("2025-12-31T20:00:00+00:00", "2026-01-01T06:00:00+00:00", rc.ANNUAL, _LONDON)
    assert not _MOD._gap_crosses_reset("2026-03-01T00:00:00+00:00", "2026-03-05T00:00:00+00:00", rc.ANNUAL, _LONDON)

def test_gap_crosses_reset_annual_endpoint_on_boundary_is_not_crossing():
    rc = _MOD.ResetClass
    # start exactly at Jan-1 00:00 local (GMT): boundary AT endpoint, not inside
    assert not _MOD._gap_crosses_reset("2025-12-31T20:00:00+00:00", "2026-01-01T00:00:00+00:00", rc.ANNUAL, _LONDON)
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — the boundary must be **strictly inside** `(a, b)`:
```python
def _gap_crosses_reset(prev_start: str, start: str, reset_class: ResetClass, tz: ZoneInfo) -> bool:
    """True if a natural reset boundary for *reset_class* falls STRICTLY within
    (prev_start, start), in local time. A reading exactly on the boundary is a
    normal reset row, not a crossed gap. LIFETIME never resets."""
    if reset_class is ResetClass.LIFETIME:
        return False
    a = _to_utc(prev_start).astimezone(tz)
    b = _to_utc(start).astimezone(tz)
    if b <= a:
        return False
    if reset_class is ResetClass.DAILY:
        first_midnight = (a + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return first_midnight < b   # strictly before b
    # ANNUAL: first Jan-1 boundary after a, strictly before b
    first_jan1 = a.replace(year=a.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_jan1 < b
```
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): _gap_crosses_reset helper (#172)"`

---

## Task 3: `_segment_coherent` helper

A buffered held run is a coherent cumulative segment when its internal adjacent deltas are each non-negative and within the bound for **that pair's own elapsed time** (`ceiling × elapsed_i`) — not one cumulative bound, which would let a single-hour internal jump up to `3×ceiling` slip through on the 3rd hold. Input is `(start, state)` tuples so per-pair elapsed is available. (Only the offset *into* the segment is bidirectional — handled in the walk, not here.)

**Files:** Modify script; Test.

- [ ] **Step 1: Failing test**
```python
def test_segment_coherent_monotonic_within_bound():
    held = [("2026-05-20T12:00:00+00:00", 1000.0),
            ("2026-05-20T13:00:00+00:00", 1002.7),
            ("2026-05-20T14:00:00+00:00", 1005.1)]   # +2.7, +2.4 over 1h each
    assert _MOD._segment_coherent(held, 25.0, _LONDON)

def test_segment_coherent_rejects_oscillation():
    held = [("2026-05-20T12:00:00+00:00", 1000.0),
            ("2026-05-20T13:00:00+00:00", 1010.0),
            ("2026-05-20T14:00:00+00:00", 1001.0)]   # internal negative delta
    assert not _MOD._segment_coherent(held, 25.0, _LONDON)

def test_segment_coherent_per_pair_elapsed_bound():
    # all hourly: a +60 internal delta over 1h exceeds 1*25 even though the
    # cumulative span (3h) would permit 75 — must be rejected.
    held = [("2026-05-20T12:00:00+00:00", 1000.0),
            ("2026-05-20T13:00:00+00:00", 1002.0),
            ("2026-05-20T14:00:00+00:00", 1062.0)]   # +60 over 1h
    assert not _MOD._segment_coherent(held, 25.0, _LONDON)

def test_segment_coherent_irregular_spacing_uses_each_gap():
    # 5h between the last pair allows a larger (but still <= 25*5) delta
    held = [("2026-05-20T12:00:00+00:00", 1000.0),
            ("2026-05-20T13:00:00+00:00", 1002.0),
            ("2026-05-20T18:00:00+00:00", 1100.0)]   # +98 over 5h <= 125
    assert _MOD._segment_coherent(held, 25.0, _LONDON)
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — bound each pair by its own elapsed:
```python
def _segment_coherent(held: list[tuple[str, float]], ceiling: float, tz: ZoneInfo) -> bool:
    """True if the held (start, state) readings form a monotonic cumulative
    segment: every adjacent internal delta is in [0, ceiling × elapsed_pair].
    The offset from the prior baseline to held[0] is evaluated by the caller
    (it may be either direction)."""
    for (sa, va), (sb, vb) in zip(held, held[1:]):
        if not (0 <= vb - va <= ceiling * _elapsed_hours(sa, sb)):
            return False
    return True
```
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): _segment_coherent helper (#172)"`

---

## Task 4: `_smear_gap` helper

Synthesise daily-boundary rows climbing linearly across a non-reset-crossing gap, so a booked multi-hour delta lands as physically-plausible per-day increments instead of one impossible spike.

**Files:** Modify script; Test.

- [ ] **Step 1: Failing test**
```python
def test_smear_gap_daily_boundaries_climb_linearly():
    # 100 -> +60 over 2026-05-20T12:00Z .. 2026-05-23T12:00Z (3 days)
    rows = _MOD._smear_gap(100.0, 60.0, "2026-05-20T12:00:00+00:00", "2026-05-23T12:00:00+00:00", _LONDON)
    # local midnights strictly inside: 05-21, 05-22, 05-23 (00:00 local == 23:00Z prev day in BST)
    assert len(rows) >= 2
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)               # monotonic climb
    assert all(100.0 < s < 160.0 for s in sums)  # strictly between start and end
    assert all(r["state"] == r["sum"] for r in rows)

def test_smear_gap_zero_or_negative_span_empty():
    assert _MOD._smear_gap(10.0, 5.0, "2026-05-20T12:00:00+00:00", "2026-05-20T12:00:00+00:00", _LONDON) == []
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
```python
def _smear_gap(
    prev_sum: float, total_delta: float, prev_start: str, end_start: str, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Daily-boundary rows climbing linearly from prev_sum across (prev_start,
    end_start). The caller emits the real end row (carrying prev_sum+total_delta);
    these fill the gap so each day shows a plausible share, not one spike."""
    start_dt = _to_utc(prev_start)
    end_dt = _to_utc(end_start)
    total = (end_dt - start_dt).total_seconds()
    if total <= 0:
        return []
    rows: list[dict[str, Any]] = []
    local = start_dt.astimezone(tz)
    day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        boundary = day.astimezone(UTC)
        if boundary >= end_dt:
            break
        frac = (boundary - start_dt).total_seconds() / total
        s = round(prev_sum + total_delta * frac, 6)
        rows.append({"start": _as_iso(boundary), "sum": s, "state": s})
        day += timedelta(days=1)
    return rows
```
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): _smear_gap daily-boundary interpolation (#172)"`

---

## Task 5: Rewrite `rebuild_sum_walk` (buffered segments)

**Files:** Modify `scripts/migrate_from_givtcp.py:389` (`rebuild_sum_walk`); Test.

The tests below are the contract — implement to satisfy all of them. The walk buffers over-bound/off-boundary readings (does not emit them immediately) and resolves each held run as transient (flush last-good) or coherent segment (re-baseline). An `events` dict out-param records `rebaseline`, `smear`, `gap_undercount`, and `unresolved` entries.

- [ ] **Step 1: Write the failing test suite**
```python
def _walk(rows, rc, ceiling, events=None):
    return _MOD.rebuild_sum_walk(rows, rc, ceiling, _LONDON, events=events)

def test_walk_accepts_genuine_climb():
    rows = [_row("2026-05-20T08:00:00+00:00", 100.0),
            _row("2026-05-20T09:00:00+00:00", 102.0),
            _row("2026-05-20T10:00:00+00:00", 105.0)]
    assert _sums(_walk(rows, _MOD.ResetClass.LIFETIME, 50.0)) == [100.0, 102.0, 105.0]

def test_walk_transient_spike_held():
    rows = [_row("2026-05-20T12:00:00+00:00", 200.0),
            _row("2026-05-20T13:00:00+00:00", 9999.0),   # 1-reading spike
            _row("2026-05-20T14:00:00+00:00", 203.0)]
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 50.0)
    assert _sums(out) == [200.0, 200.0, 203.0]
    assert [r["state"] for r in out] == [200.0, 200.0, 203.0]   # held state = last-good

def test_walk_sustained_upward_rebase_recovers_and_books_internal():
    # The live failure: climb, an ~8000 artifact jump, then genuine +2.7/+2.4 climb.
    rows = [_row("2026-05-20T10:00:00+00:00", 1000.0),
            _row("2026-05-20T11:00:00+00:00", 1003.0),
            _row("2026-05-20T12:00:00+00:00", 9003.0),   # +8000 artifact (offset)
            _row("2026-05-20T13:00:00+00:00", 9005.7),   # +2.7 internal
            _row("2026-05-20T14:00:00+00:00", 9008.1),   # +2.4 internal
            _row("2026-05-20T15:00:00+00:00", 9010.0)]   # +1.9 post-segment
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    sums = _sums(out)
    assert sums == sorted(sums)                 # monotonic, NOT flat
    assert max(b - a for a, b in zip(sums, sums[1:])) < 25.0   # offset suppressed
    # genuine accumulation retained: 1003 -> ~1003+2.7+2.4+1.9 = 1010.0
    assert abs(sums[-1] - 1010.0) < 0.01
    assert events.get("rebaseline")             # a re-baseline was recorded

def test_walk_sustained_downward_rebase_recovers():
    rows = [_row("2026-05-20T10:00:00+00:00", 9000.0),
            _row("2026-05-20T11:00:00+00:00", 9002.0),
            _row("2026-05-20T12:00:00+00:00", 10.0),    # downward meter reset (offset)
            _row("2026-05-20T13:00:00+00:00", 12.0),    # +2 internal
            _row("2026-05-20T14:00:00+00:00", 14.0)]    # +2 internal
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0)
    sums = _sums(out)
    assert sums == sorted(sums)                 # still monotonic (offset suppressed)
    assert abs((sums[-1] - sums[1]) - 4.0) < 0.01   # only the +2+2 booked after the climb

def test_walk_three_corrupt_highs_then_recovery_not_a_segment():
    # corrupt highs that are NOT a coherent climb, then return to real lower value
    rows = [_row("2026-05-20T10:00:00+00:00", 1000.0),
            _row("2026-05-20T11:00:00+00:00", 9000.0),  # corrupt
            _row("2026-05-20T12:00:00+00:00", 200.0),   # corrupt (internal delta -8800)
            _row("2026-05-20T13:00:00+00:00", 9000.0),  # corrupt
            _row("2026-05-20T14:00:00+00:00", 1002.0)]  # back to real
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0)
    # never re-baselined to a corrupt level; recovers against last-good 1000 -> 1002
    assert _sums(out) == [1000.0, 1000.0, 1000.0, 1000.0, 1002.0]

def test_walk_smears_lifetime_gap():
    # 12h gap with a +6 genuine delta (<= 25*12); LIFETIME -> smear daily
    rows = [_row("2026-05-20T12:00:00+00:00", 100.0),
            _row("2026-05-21T00:00:00+00:00", 106.0)]
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    assert _sums(out)[-1] == 106.0
    assert events.get("smear")
    # at least one synthesised intermediate row between the two
    assert len(out) > len(rows)

def test_walk_daily_midnight_gap_negative_delta_is_gap_undercount():
    # DAILY counter, gap crosses midnight, negative endpoint delta (8 -> 2)
    rows = [_row("2026-05-20T22:00:00+00:00", 8.0),     # 23:00 local
            _row("2026-05-21T02:00:00+00:00", 2.0)]     # next day, after reset
    events = {}
    out = _walk(rows, _MOD.ResetClass.DAILY, 25.0, events=events)
    # carry flat across the gap (under-count), do NOT treat as rebase/segment
    assert _sums(out) == [8.0, 8.0]
    assert events.get("gap_undercount")
    assert not events.get("rebaseline")

def test_walk_unresolved_held_tail_recorded():
    # a short over-bound run at EOF that never confirms or reverts
    rows = [_row("2026-05-20T10:00:00+00:00", 100.0),
            _row("2026-05-20T11:00:00+00:00", 9000.0),
            _row("2026-05-20T12:00:00+00:00", 9002.0)]  # only 2 held (< K=3), then EOF
    events = {}
    out = _walk(rows, _MOD.ResetClass.LIFETIME, 25.0, events=events)
    assert events.get("unresolved")             # tail flagged
    assert _sums(out) == [100.0, 100.0, 100.0]  # emitted last-good (flat) but FLAGGED

def test_walk_carries_gap_rows_none_state():
    rows = [_row("2026-05-20T12:00:00+00:00", 100.0),
            _row("2026-05-20T13:00:00+00:00", None),
            _row("2026-05-20T14:00:00+00:00", 101.0)]
    assert _sums(_walk(rows, _MOD.ResetClass.LIFETIME, 50.0)) == [100.0, 100.0, 101.0]
```
- [ ] **Step 2: Run the suite, expect FAILS** — `uv run pytest tests/test_migrate_stats_repair.py -k "walk_" -v`
- [ ] **Step 3: Implement the rewritten walk.** Replace `rebuild_sum_walk` (lines 389–444) with the buffered-segment implementation below. Key invariants: held readings are buffered (not emitted) until resolved; transient → emit last-good; coherent segment → suppress offset, book internal deltas, emit normalized state (sum); reset-crossing gap handled first; unresolved held tail recorded.

```python
def rebuild_sum_walk(
    rows: list[dict[str, Any]],
    reset_class: ResetClass,
    ceiling: float,
    tz: ZoneInfo,
    midnight_tol_hours: float = 2.0,
    events: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild a clean cumulative ``sum`` from ``state``; recover from sustained
    shifts instead of flat-lining. See the design spec for the full rationale.

    ``events`` (if given) accumulates lists under keys: ``rebaseline``, ``smear``,
    ``gap_undercount``, ``unresolved`` — surfaced by validation.
    """
    def _ev(key: str, payload: dict) -> None:
        if events is not None:
            events.setdefault(key, []).append(payload)

    out: list[dict[str, Any]] = []
    running = 0.0
    prev_state: float | None = None
    prev_start: str | None = None
    held: list[tuple[str, float]] = []  # (start, state) buffered, not yet emitted

    def _emit(start: str, sum_val: float, state_val: float) -> None:
        out.append({"start": start, "sum": round(sum_val, 6), "state": round(state_val, 6)})

    def _flush_transient() -> None:
        # The held run was a transient spike: emit each as last-good (flat).
        for s, _st in held:
            _emit(s, running, prev_state)
        held.clear()

    def _flush_segment() -> None:
        nonlocal running, prev_state, prev_start
        base = held[0][1]
        # offset (held[0] - prev_state) is suppressed; internal deltas are booked.
        for s, st in held:
            _emit(s, running + (st - base), running + (st - base))
        _ev("rebaseline", {"start": held[0][0], "offset": round(base - prev_state, 3), "held": len(held)})
        running += held[-1][1] - base
        prev_state = held[-1][1]
        prev_start = held[-1][0]
        held.clear()

    for row in rows:
        state = row.get("state")
        start = row["start"]
        if state is None:
            # gap row: carry running forward (do not resolve held on a None row).
            _emit(start, running, prev_state if prev_state is not None else running)
            continue
        if prev_state is None:
            running = float(state)
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue

        elapsed = _elapsed_hours(prev_start, start)
        bound = ceiling * elapsed
        # (1) Reset-crossing gap FIRST, before any delta-sign branch.
        if elapsed > 1 and _gap_crosses_reset(prev_start, start, reset_class, tz):
            _flush_transient()
            _emit(start, running, running)        # carry flat across the gap
            _ev("gap_undercount", {"start": start, "from": prev_start})
            prev_state = float(state)
            prev_start = start
            continue

        delta = state - prev_state
        # (2) Accept genuine (possibly multi-hour) accumulation.
        if 0 <= delta <= bound:
            _flush_transient()
            if elapsed > 1:
                out.extend(_smear_gap(running, delta, prev_start, start, tz))
                _ev("smear", {"start": start, "energy": round(delta, 3), "hours": round(elapsed, 1)})
            running += delta
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue
        # (3) Boundary reset (intra-reading, no gap).
        if delta < 0 and _is_reset_boundary(start, reset_class, tz, midnight_tol_hours):
            _flush_transient()
            running += state
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue
        # (4) Otherwise: buffer as held; try to confirm a coherent segment.
        #     Coherence bounds each held pair by its OWN elapsed time (passes the
        #     (start, state) tuples + ceiling), not the single cumulative `bound`.
        held.append((start, float(state)))
        if len(held) >= _REBASELINE_HOLDS and _segment_coherent(held, ceiling, tz):
            _flush_segment()

    if held:
        _ev("unresolved", {"start": held[0][0], "count": len(held)})
        _flush_transient()  # emit last-good; the recorded event makes the gate refuse
    return out
```
Note: `held_states` is computed but only `_segment_coherent([h[1] for h in held], bound)` is used (segment internal coherence). The offset to the segment is intentionally not constrained in direction; remove the unused `held_states` line if ruff flags it.
- [ ] **Step 4: Run the suite, expect PASS** — `uv run pytest tests/test_migrate_stats_repair.py -k "walk_" -v`. Then the whole file. Fix the implementation (not the tests) until green.
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): buffered-segment rebuild walk recovers from sustained shifts (#172)"`

---

## Task 6: `find_flat_line_spans` (gap-undercount-aware)

**Files:** Modify script (validation region); Test.

Duration is measured from **timestamps**, not row count (8 hourly rows span 7 hours), with **epsilon** equality (synthetic/float sums) and `start`+`end` so the caller can exempt by interval overlap.

- [ ] **Step 1: Failing test**
```python
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
    rows = [{"start": "2026-05-20T00:00:00+00:00", "sum": 100.0},
            {"start": "2026-05-20T08:00:00+00:00", "sum": 100.0}]
    spans = _MOD.find_flat_line_spans(rows, min_hours=6)
    assert spans and abs(spans[0]["hours"] - 8.0) < 1e-9
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — duration from timestamps, epsilon equality:
```python
_FLAT_EPSILON = 1e-6

def find_flat_line_spans(rows: list[dict[str, Any]], min_hours: int = _FLAT_LINE_MIN_HOURS) -> list[dict[str, Any]]:
    """Maximal runs of consecutive equal-sum rows whose DURATION (from timestamps,
    not row count) is >= min_hours. Epsilon comparison handles float/synthetic
    sums. Returns {start, end, hours}; the caller exempts spans that overlap a
    recorded gap_undercount interval."""
    spans: list[dict[str, Any]] = []
    run_start = 0
    for i in range(1, len(rows) + 1):
        changed = i == len(rows) or abs((rows[i].get("sum") or 0.0) - (rows[i - 1].get("sum") or 0.0)) > _FLAT_EPSILON
        if not changed:
            continue
        last = i - 1
        if last > run_start:  # at least two rows in the run
            duration = (_to_utc(rows[last]["start"]) - _to_utc(rows[run_start]["start"])).total_seconds() / 3600.0
            if duration >= min_hours:
                spans.append({"start": rows[run_start]["start"], "end": rows[last]["start"], "hours": round(duration, 6)})
        run_start = i
    return spans
```
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): find_flat_line_spans under-count detector (#172)"`

---

## Task 7: reset-aware movement helper + `compare_source_movement`

Two pieces: a **reset-aware movement** helper (genuine accumulation over a window, counting post-reset state at legitimate resets — not `max(0, Δ)`, which loses it), and the comparison, which excludes only **upward** rebase offsets (a downward offset was never part of positive movement, so subtracting it is wrong).

**Files:** Modify script; Test.

- [ ] **Step 1: Failing tests**
```python
def test_reset_aware_movement_counts_post_reset():
    rc = _MOD.ResetClass
    # DAILY: 2,5 then reset to 0,3 -> genuine movement = (5-2) + 3 = 6, not 3
    rows = [_row("2026-05-20T22:00:00+00:00", 2.0),
            _row("2026-05-20T22:30:00+00:00", 5.0),
            _row("2026-05-20T23:00:00+00:00", 0.0),   # local-midnight reset (BST)
            _row("2026-05-21T00:00:00+00:00", 3.0)]
    assert abs(_MOD._reset_aware_movement(rows, rc.DAILY, _LONDON) - 6.0) < 1e-6

def test_reset_aware_movement_lifetime_is_positive_deltas():
    rc = _MOD.ResetClass
    rows = [_row("2026-05-20T10:00:00+00:00", 100.0),
            _row("2026-05-20T11:00:00+00:00", 103.0),
            _row("2026-05-20T12:00:00+00:00", 105.0)]
    assert abs(_MOD._reset_aware_movement(rows, rc.LIFETIME, _LONDON) - 5.0) < 1e-6

def test_compare_source_movement_excludes_upward_offset_only():
    # source moved 100 genuine + an 8000 upward rebase offset; rebuilt has 100
    res = _MOD.compare_source_movement(source_movement=8100.0, rebuilt_movement=100.0,
                                       upward_offsets=8000.0, tol_pct=5.0)
    assert res["flagged"] is False and abs(res["expected"] - 100.0) < 1e-9

def test_compare_source_movement_downward_offset_not_subtracted():
    # a downward rebase: its offset is negative and was never in positive movement,
    # so upward_offsets is 0 -> expected stays 100, matches rebuilt 100
    res = _MOD.compare_source_movement(source_movement=100.0, rebuilt_movement=100.0,
                                       upward_offsets=0.0, tol_pct=5.0)
    assert res["flagged"] is False and res["expected"] == 100.0

def test_compare_source_movement_flags_real_divergence():
    res = _MOD.compare_source_movement(source_movement=100.0, rebuilt_movement=60.0,
                                       upward_offsets=0.0, tol_pct=5.0)
    assert res["flagged"] is True and abs(res["expected"] - 100.0) < 1e-9
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** the reset-aware movement helper and the comparison:
```python
def _reset_aware_movement(rows: list[dict[str, Any]], reset_class: ResetClass, tz: ZoneInfo) -> float:
    """Genuine accumulation across *rows*: positive consecutive deltas, plus the
    post-reset state at a legitimate reset boundary (a reset drops the raw value,
    so max(0, Δ) would lose that day's pre-reset accumulation otherwise)."""
    total = 0.0
    prev = None
    prev_start = None
    for r in rows:
        st = r.get("state")
        if st is None:
            continue
        if prev is not None:
            if st < prev and _is_reset_boundary(r["start"], reset_class, tz, 2.0):
                total += st                      # post-reset accumulation
            elif st >= prev:
                total += st - prev               # genuine forward movement
            # an off-boundary drop contributes nothing (corruption)
        prev = st
        prev_start = r["start"]
    return total

def compare_source_movement(
    source_movement: float, rebuilt_movement: float, upward_offsets: float, tol_pct: float = _MOVEMENT_TOLERANCE_PCT
) -> dict[str, Any]:
    """Compare rebuilt movement to *cleaned expected* = source movement minus the
    recorded UPWARD rebase offsets the rebuild intentionally dropped. Downward
    offsets were never in the positive source movement, so they are NOT
    subtracted. Movements are reset-aware (see _reset_aware_movement) over the
    same aligned window."""
    expected = source_movement - upward_offsets
    denom = abs(expected) if abs(expected) > 1e-9 else 1.0
    diff_pct = abs(rebuilt_movement - expected) / denom * 100.0
    return {"expected": round(expected, 3), "rebuilt": round(rebuilt_movement, 3),
            "diff_pct": round(diff_pct, 2), "flagged": diff_pct > tol_pct}
```
(`prev_start` is retained for parity with the walk's pattern; drop it if ruff flags it unused.)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): reset-aware movement + upward-offset-excluded comparison (#172)"`

---

## Task 8: `MigrationResult` candidate/source/event fields + populate in `migrate_entity`

**Files:** Modify `scripts/migrate_from_givtcp.py:816` (`MigrationResult`) and `:909-933` (rebuild branch of `migrate_entity`). Test.

- [ ] **Step 1: Extend `MigrationResult`** — add to `__slots__` and `__init__`: `rebuilt_rows: list | None = None`, `events: dict | None = None`, `source_movement: float = 0.0`, `upward_offsets: float = 0.0`, `post_movement: float = 0.0` (rebuilt movement over the post-cutover window), `ge_post_movement: float = 0.0` (original GE source movement over the same post-cutover window, for the preservation comparison), `metadata: dict | None = None` (the import metadata, so Phase B can write without rebuilding). Initialise all in `__init__`.
- [ ] **Step 2: Populate in `migrate_entity`'s rebuild branch.** After computing `ceiling`, build the candidate but DO NOT write here (writing moves to Phase B):
```python
        events: dict[str, list] = {}
        merged = rebuild_sum_walk(merged_states, reset_class, ceiling, tz, events=events)
        r.rebuilt_rows = merged
        r.events = events
        # reset-aware source movement over the pre-cutover window, the UPWARD
        # offsets the rebuild suppressed (cleaned comparison), and the rebuilt
        # movement over the post-cutover window (GE-preservation check)
        pre = [s for s in merged_states if _to_utc(s["start"]) < cutover]
        r.source_movement = _reset_aware_movement(pre, reset_class, tz)
        r.upward_offsets = sum(e["offset"] for e in events.get("rebaseline", []) if e["offset"] > 0)
        post = [s for s in merged if _to_utc(s["start"]) >= cutover]
        r.post_movement = _reset_aware_movement(post, reset_class, tz)
        # original GE rows over the SAME post-cutover window, for preservation check
        r.ge_post_movement = _reset_aware_movement(ge_post, reset_class, tz)
        r.sum_at_cutover = next((row["sum"] for row in merged if _to_utc(row["start"]) >= cutover), None)
        r.metadata = {"has_mean": False, "has_sum": True, "name": None,
                      "source": "recorder", "statistic_id": ge_id, "unit_of_measurement": ge_unit}
    r.merged_rows = len(merged)
    r.status = "candidate"   # built, not yet validated/written
    return r
```
   Remove the old `if not apply: dry_run` / `clear+import` block from `migrate_entity` — writing is now exclusively Phase B (Task 9). The `trust_source_sums` branch still builds `merged` + sets `r.rebuilt_rows`/`r.metadata` and `status="candidate"` (no events).
- [ ] **Step 3: Test** the populate path with the existing `_FakeWS` dry-run pattern in `tests/test_script_entity_refs.py`: a migrate_entity call returns `status == "candidate"` with `rebuilt_rows` populated and `events` present. Update existing migrate_entity tests that asserted `status == "dry_run"`/`"migrated"` to the new `"candidate"` status (status now reflects build, not write).
- [ ] **Step 4: Run** `uv run pytest tests/test_script_entity_refs.py -v`; fix.
- [ ] **Step 5: Commit** — `git commit -am "refactor(migrate): migrate_entity builds a candidate (no write); carry rows/events/movement (#172)"`

---

## Task 9: Two-phase apply in `run` (validate-all → write-all-or-abort)

**Files:** Modify `scripts/migrate_from_givtcp.py` (`run`, the execution loop ~1340+); add a `write_candidate` helper; Test.

- [ ] **Step 1: Add `write_candidate` helper** (the only writer; mirrors the old apply block):
```python
async def write_candidate(ws: HAWebSocket, r: MigrationResult) -> None:
    """Phase B: clear + import one approved candidate. Not transactional across
    entities — the caller aborts + reports on the first failure (backup recovers)."""
    await ws.clear_statistics([r.ge_id])
    await ws.import_statistics(metadata=r.metadata, stats=r.rebuilt_rows)
```
- [ ] **Step 2: Restructure `run`'s apply flow** into three stages. **Phase A (build+validate):** build every candidate (the loop calling `migrate_entity`, now `status="candidate"`), then `run_validation(..., applied=False)` over the candidates for blocking findings + the report. `blocking` = any unexplained flat span / movement divergence / unresolved-held / incoherent across candidates. If `applying` and `blocking`: print the gate refusal, write nothing, return non-zero. **Phase B (write):** if clean, iterate candidates calling `write_candidate`; on the first exception, print exactly which entities were already written, which was mid-write, and the restore-from-backup instruction, then return non-zero (do **not** continue). **Phase C (post-write verify):** after all writes succeed, re-read each stored series and assert it matches the approved candidate (same row count and per-row `sum` within epsilon, via a `verify_written(ws, r)` helper). On any mismatch or read failure, fail loudly with the backup-recovery instruction and return non-zero — do **not** report success. Only when Phase C passes set `status="migrated"`. Dry-run (`not applying`) stops after Phase A.
```python
async def verify_written(ws: HAWebSocket, r: MigrationResult) -> bool:
    """Phase C: re-read the stored series and confirm it matches the approved
    candidate — row count, per-row normalized `start` timestamp, AND per-row sum
    within epsilon (equal sums with shifted/reordered timestamps must NOT pass)."""
    raw = await ws.get_statistics([r.ge_id], _EPOCH)
    stored = [_normalise(s) for s in raw.get(r.ge_id, [])]
    if len(stored) != len(r.rebuilt_rows):
        return False
    return all(
        a["start"] == b["start"]
        and abs((a.get("sum") or 0.0) - (b.get("sum") or 0.0)) <= _FLAT_EPSILON
        for a, b in zip(stored, r.rebuilt_rows)
    )
```
- [ ] **Step 3: Test the gate + mid-Phase-B failure** in `tests/test_migrate_stats_repair.py` with a fake WS:
```python
def test_phase_b_aborts_on_blocking_candidate(monkeypatch):
    # Build two candidates; one has an unresolved-held event -> blocking.
    # Assert no write_candidate calls occurred (gate refused before Phase B).
    ...
def test_phase_b_mid_failure_reports_and_stops():
    # Fake ws.import_statistics raises on the 2nd entity.
    # Assert: entity 1 written, entity 2 attempted, loop stops (entity 3 NOT written),
    # the returned/printed report names written vs cleared and points to the backup.
    ...

def test_phase_c_detects_stored_mismatch():
    # Writes "succeed", but the fake WS read-back differs from the candidate.
    # Cover BOTH: (a) an altered sum, and (b) SAME sums with shifted timestamps
    # (the latter must also fail — verify compares start timestamps too).
    # Assert: verify_written returns False -> run fails loudly with backup guidance,
    # status is NOT "migrated", non-zero exit.
    ...
```
(Write these against the actual `run`/helper structure you build; assert via a fake WS recording `clear_statistics`/`import_statistics` calls, a configurable read-back, and the printed report.)
- [ ] **Step 4: Run** the tests; fix until green.
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): two-phase apply — validate all, then write-all-or-abort (#172)"`

---

## Task 10: `run_validation` candidate-based + new findings + accepted/blocking

**Files:** Modify `scripts/migrate_from_givtcp.py:996` (`run_validation`). Test.

- [ ] **Step 1:** Change `run_validation` to validate the **candidate** (`r.rebuilt_rows`) rather than re-reading HA when those rows are present (both dry-run and the Phase-A gate). For each candidate compute: `gaps` (existing); the raw `find_flat_line_spans(rows)`, then **clip** each span by the recorded `gap_undercount` intervals and keep only the **residual unexplained contiguous** portions — a span is blocking only if a residual portion's duration ≥ `min_hours` (so a short accepted reset gap that merely touches a multi-day flat does NOT exempt the whole flat); `compare_source_movement(r.source_movement, <pre-cutover candidate movement>, r.upward_offsets)`; a post-cutover GE-preservation check comparing `r.post_movement` (rebuilt) against `r.ge_post_movement` (original GE source, Task 8) over the same window; plus the `events`. Build `findings[ge_id]` from all of these. Return the report exit code AND a `blocking` flag = any residual unexplained flat ≥ threshold, `source_comparison.flagged`, post-cutover divergence, or non-empty `unresolved`. Add a `_unexplained_flat_portions(span, gap_intervals)` helper (subtract covered intervals, return residual `[start,end]` contiguous pieces with durations) and unit-test it: **a long flat containing one short accepted reset gap still has a residual ≥ threshold → blocking**, while a flat fully covered by gap intervals → exempt.
- [ ] **Step 2:** Keep `_repairable`/`--repair-residue` working against candidates.
- [ ] **Step 3: Test** (fake WS / direct call): a candidate with a flat span not covered by a gap_undercount → blocking; a candidate whose only flat span is covered by a recorded gap_undercount → not blocking (warned); an `unresolved` event → blocking.
- [ ] **Step 4: Run**; fix.
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): validate the rebuilt candidate; classify accepted vs blocking findings (#172)"`

---

## Task 11: `format_validation_report` renders new finding types

**Files:** Modify `scripts/migrate_from_givtcp.py:682` (`format_validation_report`). Test.

- [ ] **Step 1: Failing test** — extend `test_format_validation_report_*` so a findings dict containing `flat_lines`, `rebaseline`, `smear`, `gap_undercount`, `unresolved`, and a flagged `source_comparison` renders a line for each, marks `gap_undercount`/`smear`/`rebaseline` as accepted (warn) and `flat_lines`(unexplained)/`unresolved`/divergence as blocking, and returns a non-zero exit only for blocking findings.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** the additional render branches + accepted/blocking tally (extend the existing loop; `exit_code` non-zero iff a blocking finding exists; accepted findings print but don't set the code).
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(migrate): render flat-line/rebaseline/smear/gap/unresolved findings (#172)"`

---

## Task 12: Live-failure acceptance fixture + full regression

**Files:** Test `tests/test_migrate_stats_repair.py`.

- [ ] **Step 1: Add an end-to-end acceptance test** reproducing the live failure through the candidate path: build merged states with the ~8000 artifact jump + genuine climb (as in Task 5's `test_walk_sustained_upward_rebase_*` but routed through a fake-WS `migrate_entity`), assert the candidate is monotonic/not-flat, totals reconcile with cleaned source movement (via `compare_source_movement`), validation is non-blocking (a recorded rebaseline, no unexplained flat), and — through the real apply path with a fake WS that reads back what it wrote — **Phase C `verify_written` passes**; then a variant where the read-back is altered asserts Phase C **fails loudly** (non-zero, backup guidance, status not "migrated"). Also assert a DAILY day-crossing gap yields a `gap_undercount` (accepted) and an unexplained-flat candidate is blocking (gate refuses, no writes).
- [ ] **Step 2: Run** `uv run pytest tests/test_migrate_stats_repair.py tests/test_script_entity_refs.py -v`; then `uv run pytest -q`. Expected: all pass.
- [ ] **Step 3: Commit** — `git commit -am "test(migrate): live-failure acceptance for sustained-shift recovery (#172)"`

---

## Task 13: Docs + final verification

**Files:** Modify `docs/migration-from-givtcp.md`, module docstring; run full checks.

- [ ] **Step 1:** Document in `docs/migration-from-givtcp.md` and the module docstring: the buffered-segment recovery, reset-aware smear vs DAILY/ANNUAL `gap_undercount`, the two-phase validate-all-before-any-write gate (non-transactional; backup is the recovery net), and that `--apply` still requires `--max-kw`.
- [ ] **Step 2: Verify** — `uv run pytest -q` (all pass); `uv run ruff check scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py && uv run ruff format --check scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py` (clean). Report counts.
- [ ] **Step 3: Commit** — `git commit -am "docs(migrate): document sustained-shift recovery + two-phase gate (#172)"`

---

## Self-review notes

- **Spec coverage:** time-scaled bound (T1/T5), reset-crossing-first (T2/T5), coherent bidirectional-offset/non-negative-internal segment (T3/T5), book-internal/suppress-offset + normalized state (T5), reset-aware smear (T4/T5), unresolved-held refusal (T5/T10), flat-line + offset-excluded movement comparison (T6/T7/T10), two-phase whole-run gate + mid-Phase-B handling (T9), dry-run-validates-candidate (T10), live-failure fixture (T5/T12). All rev-4 decisions mapped.
- **Open implementation detail (resolve in T9/T10):** the exact wiring of `blocking` from `run_validation` into `run`'s Phase-A/B decision is described, not coded line-for-line, because it depends on the final shape of `run`; the tests in T9/T10 pin the required behaviour. Implementer/reviewer to confirm against the real `run`.
- **Normalized state caveat:** for LIFETIME counters emitting `state == sum` is correct; for DAILY counters this is a simplification (state would normally reset daily). The candidate's `sum` (what charts use) is correct regardless; if a reviewer finds DAILY `state` fidelity matters, raise before merge (the tests assert on `sum`, with `state` checked only for the held-last-good guarantee).
```
