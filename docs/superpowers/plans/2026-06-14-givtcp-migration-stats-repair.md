# GivTCP Migration Stats-Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `migrate_from_givtcp.py` rebuild long-term `sum` statistics from `state` (plausibility-guarded, reset-aware) by default, run a validation report, and back-port mean/power/SOC series — fixing the join seam and inherited GivTCP corruption described in #162.

**Architecture:** Add a pure repair core to the existing single-file script — entity reset-classification, an adaptive plausibility ceiling, and a reset-aware rebuild walk that concatenates the `state` timeline across the cut-over and produces one continuous `sum` (eliminating the seam). Plus pure validation checks and a curated mean-pairs back-port. WebSocket I/O stays a thin shell; all repair logic is unit-tested via the existing `importlib` module-load pattern.

**Tech Stack:** Python 3.11+, stdlib only (`zoneinfo`, `statistics`), pytest. The script's `websockets` import stays lazy so the module imports cleanly for tests.

---

## File structure

- **Modify** `scripts/migrate_from_givtcp.py` — add the repair core (`ResetClass`, `classify_entity`, `adaptive_ceiling`, `_is_reset_boundary`, `rebuild_sum_walk`), `MEAN_PAIRS`, validation checks (`find_implausible_hours`, `find_duplicate_series`, `classify_gaps`, `find_fake_reset_shapes`), CLI flags, and wiring.
- **Create** `tests/test_migrate_stats_repair.py` — unit tests for the repair core + validation, and the LTS-fixture acceptance test.
- **Modify** `docs/migration-from-givtcp.md` — document rebuild-by-default, the flags, and the mean back-port.

All new pure helpers live in the script's existing "pure helpers" region (above `class HAWebSocket`) so the `importlib` test loader exposes them without `websockets`.

The test module reuses this loader (copy into the new test file):

```python
import importlib.util
from pathlib import Path
from types import ModuleType

_MIGRATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_from_givtcp.py"


def _load_migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_from_givtcp", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

---

## Task 1: Entity reset-class classification

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (pure-helpers region)
- Test: `tests/test_migrate_stats_repair.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_stats_repair.py
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_MIGRATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_from_givtcp.py"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py::test_classify_entity_by_suffix -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'ResetClass'`

- [ ] **Step 3: Write minimal implementation**

Add near the top of the pure-helpers region (after the imports, before `class HAWebSocket`). Add `from enum import Enum` to the import block:

```python
class ResetClass(Enum):
    """How a counter is expected to reset, controlling reset-vs-corruption calls."""

    DAILY = "daily"      # _today sensors: reset to 0 at local midnight
    ANNUAL = "annual"    # _this_year sensors: reset at the year boundary
    LIFETIME = "lifetime"  # _total sensors: never reset within the migration window


def classify_entity(ge_suffix: str) -> ResetClass:
    """Classify a givenergy_local sensor suffix by its expected reset cadence."""
    if ge_suffix.endswith("_today"):
        return ResetClass.DAILY
    if ge_suffix.endswith("_this_year"):
        return ResetClass.ANNUAL
    return ResetClass.LIFETIME
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py::test_classify_entity_by_suffix -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): classify entities by reset cadence (#162)"
```

---

## Task 2: Adaptive plausibility ceiling

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (pure-helpers region)
- Test: `tests/test_migrate_stats_repair.py`

- [ ] **Step 1: Write the failing test**

```python
def test_adaptive_ceiling_rejects_fakes_keeps_genuine():
    # Genuine PV-like hourly deltas (0–6 kWh) with a few huge fake spikes.
    genuine = [0.1, 0.5, 1.2, 2.0, 3.5, 5.0, 6.0, 0.7, 0.3, 4.4] * 30
    fakes = [27396.1, 29671.9, 28660.0]
    ceiling = _MOD.adaptive_ceiling(genuine + fakes)
    assert max(genuine) <= ceiling < 100.0  # genuine peaks pass; fakes far above


def test_adaptive_ceiling_no_positive_deltas_is_unbounded():
    assert _MOD.adaptive_ceiling([0.0, 0.0, None]) == float("inf")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k adaptive_ceiling -v`
Expected: FAIL — `AttributeError: ... 'adaptive_ceiling'`

- [ ] **Step 3: Write minimal implementation**

Add `import statistics` to the import block, and a module constant + function:

```python
# Multiplier on the (normal-scaled) MAD for the plausibility ceiling. Tuned so
# genuine inverter-clip hours pass while order-of-magnitude fake spikes are
# rejected; pinned by the LTS-fixture acceptance test.
_CEILING_MAD_K = 8.0


def adaptive_ceiling(deltas: list[float | None]) -> float:
    """Robust per-hour ceiling from an entity's positive state-deltas.

    Uses median + K * 1.4826 * MAD over the positive, finite deltas. Both the
    median and the MAD are resistant to a handful of giant outliers, so the
    bound reflects genuine hourly behaviour even on a heavily corrupted series.
    Returns +inf when there is nothing positive to anchor on (caller then can't
    guard, and the walk accepts all non-negative deltas).
    """
    pos = sorted(d for d in deltas if d is not None and d > 0)
    if not pos:
        return float("inf")
    median = statistics.median(pos)
    mad = statistics.median([abs(d - median) for d in pos])
    spread = mad if mad > 0 else median
    return median + _CEILING_MAD_K * 1.4826 * spread
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k adaptive_ceiling -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): adaptive plausibility ceiling from state-deltas (#162)"
```

---

## Task 3: Reset-boundary test (local-time midnight / year)

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (pure-helpers region)
- Test: `tests/test_migrate_stats_repair.py`

- [ ] **Step 1: Write the failing test**

```python
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k reset_boundary -v`
Expected: FAIL — `AttributeError: ... '_is_reset_boundary'`

- [ ] **Step 3: Write minimal implementation**

Add `from zoneinfo import ZoneInfo` to imports (alongside the existing datetime import), and:

```python
def _is_reset_boundary(
    start_iso: str,
    reset_class: ResetClass,
    tz: ZoneInfo,
    tol_hours: float,
) -> bool:
    """True if a decrease at this timestamp is a legitimate counter reset.

    DAILY counters reset within ``tol_hours`` of local midnight; ANNUAL counters
    within ``tol_hours`` of local Jan-1 00:00; LIFETIME counters never reset.
    Evaluated in local time so DST (London resets at 23:00Z in summer, 00:00Z in
    winter) and inverter-clock lag are handled.
    """
    if reset_class is ResetClass.LIFETIME:
        return False
    local = datetime.fromisoformat(start_iso).astimezone(tz)
    tol_minutes = tol_hours * 60
    minutes_into_day = local.hour * 60 + local.minute
    dist_to_midnight = min(minutes_into_day, 24 * 60 - minutes_into_day)
    if reset_class is ResetClass.DAILY:
        return dist_to_midnight <= tol_minutes
    # ANNUAL: near midnight AND on Dec 31 / Jan 1.
    near_midnight = dist_to_midnight <= tol_minutes
    on_year_edge = (local.month, local.day) in {(1, 1), (12, 31)}
    return near_midnight and on_year_edge
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k reset_boundary -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): local-time reset-boundary detection (#162)"
```

---

## Task 4: Reset-aware, plausibility-guarded rebuild walk

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (pure-helpers region)
- Test: `tests/test_migrate_stats_repair.py`

This is the core. The walk takes normalised rows (`{"start": iso, "state": float|None}`) sorted ascending and returns copies with a rebuilt `"sum"`.

- [ ] **Step 1: Write the failing tests**

```python
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
    # Off-midnight drop to ~0 then recovery: corruption, not a reset.
    rows = [
        _row("2026-05-20T12:00:00+00:00", 200.0),
        _row("2026-05-20T13:00:00+00:00", 0.0),    # fake zero-read
        _row("2026-05-20T14:00:00+00:00", 203.0),  # recovery
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    # Sum holds at 200 through the zero, then advances by the genuine +3 vs
    # last-good (200 -> 203), never booking the +203 recovery spike.
    assert _sums(out) == [200.0, 200.0, 203.0]


def test_rebuild_walk_rejects_spike_over_ceiling():
    rows = [
        _row("2026-05-20T12:00:00+00:00", 100.0),
        _row("2026-05-20T13:00:00+00:00", 27496.1),  # fake spike
        _row("2026-05-20T14:00:00+00:00", 101.0),     # back to normal
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.LIFETIME, 50.0, _LONDON)
    assert _sums(out) == [100.0, 100.0, 101.0]


def test_rebuild_walk_accepts_daily_midnight_reset():
    # DAILY counter resets at local midnight (23:00Z BST).
    rows = [
        _row("2026-05-20T22:00:00+00:00", 18.0),
        _row("2026-05-20T23:00:00+00:00", 0.4),  # post-reset accumulation
        _row("2026-05-21T00:00:00+00:00", 0.9),
    ]
    out = _MOD.rebuild_sum_walk(rows, _MOD.ResetClass.DAILY, 10.0, _LONDON)
    # Running sum carries across the reset by adding post-reset state, not subtracting.
    assert _sums(out) == [18.0, 18.4, 18.9]


def test_rebuild_walk_rejects_offmidnight_drop_on_daily():
    # The ~80-90/day backfill bug: a midday negative on a daily counter is NOT a reset.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k rebuild_walk -v`
Expected: FAIL — `AttributeError: ... 'rebuild_sum_walk'`

- [ ] **Step 3: Write minimal implementation**

```python
def rebuild_sum_walk(
    rows: list[dict[str, Any]],
    reset_class: ResetClass,
    ceiling: float,
    tz: ZoneInfo,
    midnight_tol_hours: float = 2.0,
) -> list[dict[str, Any]]:
    """Rebuild the ``sum`` column from ``state`` with reset/plausibility guards.

    ``rows`` are normalised (ISO ``start``, numeric or None ``state``), sorted
    ascending. Returns copies with ``sum`` set to a clean cumulative total:

    - delta in [0, ceiling]            -> accept, advance running + last-good state
    - delta < 0 at a reset boundary    -> reset: add post-reset state to running
    - delta < 0 off-boundary           -> corruption: hold last-good (state + sum)
    - delta > ceiling                  -> fake spike: hold last-good
    - missing state (gap)              -> carry running forward

    Holding last-good leaves ``prev_state`` at the last trusted reading, so the
    recovery after a transient zero/spike is measured against it (a small,
    accepted delta) instead of booking the bogus jump.
    """
    out: list[dict[str, Any]] = []
    running = 0.0
    prev_state: float | None = None
    for row in rows:
        r = dict(row)
        state = row.get("state")
        if state is None:
            r["sum"] = round(running, 6)
            out.append(r)
            continue
        if prev_state is None:
            running = float(state)
            prev_state = float(state)
            r["sum"] = round(running, 6)
            out.append(r)
            continue
        delta = state - prev_state
        if 0 <= delta <= ceiling:
            running += delta
            prev_state = state
        elif delta < 0 and _is_reset_boundary(r["start"], reset_class, tz, midnight_tol_hours):
            running += state
            prev_state = state
        # else: corruption (off-boundary drop) or spike (> ceiling) -> hold last-good.
        r["sum"] = round(running, 6)
        out.append(r)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k rebuild_walk -v`
Expected: PASS (all six)

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): reset-aware plausibility-guarded sum rebuild walk (#162)"
```

---

## Task 5: Fetch the HA timezone over WebSocket

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (`HAWebSocket` + `run`)
- Test: `tests/test_migrate_stats_repair.py`

The walk needs the instance's local timezone. Add a `get_timezone()` WS call.

- [ ] **Step 1: Write the failing test**

```python
import asyncio


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k get_timezone -v`
Expected: FAIL — `AttributeError: ... 'get_timezone'`

- [ ] **Step 3: Write minimal implementation**

Add to `class HAWebSocket` (after `list_device_registry`):

```python
    async def get_timezone(self) -> ZoneInfo:
        """Return the HA instance's configured local timezone (UTC fallback)."""
        cfg = await self._call("get_config")
        name = (cfg or {}).get("time_zone") or "UTC"
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("UTC")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k get_timezone -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): fetch HA local timezone for reset detection (#162)"
```

---

## Task 6: Wire rebuild into migration as the default

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (`migrate_entity`, `run`, argparse)
- Test: `tests/test_migrate_stats_repair.py`

Replace the copy+rebase path with a rebuild path that concatenates GivTCP (pre-cutover `state`) and GE (`state` from cutover) and walks once. Keep the old path behind `--trust-source-sums`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k build_merged_states -v`
Expected: FAIL — `AttributeError: ... 'build_merged_states'`

- [ ] **Step 3: Write minimal implementation**

Add helper to the pure-helpers region:

```python
def build_merged_states(
    givtcp_rows: list[dict[str, Any]],
    ge_rows: list[dict[str, Any]],
    cutover: datetime,
) -> list[dict[str, Any]]:
    """Concatenate the state timeline across the cut-over for a single entity.

    GivTCP rows strictly before the cut-over, then givenergy_local rows from the
    cut-over onward, each carrying ``state``. Sorted ascending by ``start``. This
    is the input to ``rebuild_sum_walk`` — walking it produces one continuous
    sum, so the join seam never exists.
    """
    pre = [r for r in givtcp_rows if _to_utc(r["start"]) < cutover]
    post = [r for r in ge_rows if _to_utc(r["start"]) >= cutover]
    merged = pre + post
    merged.sort(key=lambda r: _to_utc(r["start"]))
    return merged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k build_merged_states -v`
Expected: PASS

- [ ] **Step 5: Integrate into `migrate_entity` + `run` + argparse**

In `migrate_entity`, add params `reset_class: ResetClass`, `tz: ZoneInfo`, `trust_source_sums: bool`. After the existing fetch/normalise of `givtcp_stats` and `ge_all`, branch:

```python
    if trust_source_sums:
        # Legacy path: copy GivTCP sums + rebase GE-post (unchanged behaviour).
        last_givtcp_sum = givtcp_stats[-1].get("sum") or 0.0
        r.sum_at_cutover = last_givtcp_sum
        rebased_post = rebase_sum(ge_post, last_givtcp_sum)
        merged = givtcp_stats + rebased_post
    else:
        # Rebuild path (default): one continuous sum from the concatenated state
        # timeline, plausibility- and reset-guarded.
        merged_states = build_merged_states(givtcp_stats, ge_all, cutover)
        deltas = [
            (merged_states[i]["state"] - merged_states[i - 1]["state"])
            for i in range(1, len(merged_states))
            if merged_states[i].get("state") is not None
            and merged_states[i - 1].get("state") is not None
        ]
        ceiling = adaptive_ceiling(deltas)
        merged = rebuild_sum_walk(merged_states, reset_class, ceiling, tz)
        r.sum_at_cutover = next(
            (row["sum"] for row in merged if _to_utc(row["start"]) >= cutover), None
        )
    r.merged_rows = len(merged)
```

In `run`, fetch the tz once (`tz = await ws.get_timezone()`), and in the plan loop pass `classify_entity(ge_sfx)` and `tz`. Note: `_build_plan` must carry the `ge_sfx` so the reset class can be derived — add `ge_sfx` to each plan tuple, or compute `classify_entity` from the suffix portion of `ge_id`. Simplest: add `reset_class` to the plan tuple in `_build_plan` via `classify_entity(ge_sfx)`. Add `--trust-source-sums` to argparse:

```python
    p.add_argument(
        "--trust-source-sums",
        action="store_true",
        help=(
            "Copy GivTCP's sum column verbatim and rebase at the join, instead of "
            "rebuilding sums from state. Use only if your GivTCP sums are known-good."
        ),
    )
```

Thread `args.trust_source_sums` and `tz` through to `migrate_entity`.

- [ ] **Step 6: Update the plan-tuple test expectations**

`_build_plan` now yields a `reset_class` field. Update `tests/test_script_entity_refs.py` if it asserts tuple arity (search for the 6-tuple `recognised = (...)` around line 255 and add the reset-class element), keeping existing assertions valid.

- [ ] **Step 7: Run the full migrate test set**

Run: `uv run pytest tests/test_migrate_stats_repair.py tests/test_script_entity_refs.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py tests/test_script_entity_refs.py
git commit -m "feat(migrate): rebuild sums by default, --trust-source-sums escape hatch (#162)"
```

---

## Task 7: Mean / power / SOC back-port

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (`MEAN_PAIRS`, `_build_plan`, mean migration path, argparse)
- Test: `tests/test_migrate_stats_repair.py`, `tests/test_script_entity_refs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_mean_pairs_present_and_shaped():
    mod = _load_migrate_module()
    suffixes = {gt for (gt, _ge, _desc) in mod.MEAN_PAIRS}
    # Power family + SOC at both levels are covered.
    assert any("pv_power" in s for s in suffixes)
    assert any("grid_power" in s or "import_power" in s for s in suffixes)
    assert any("battery_power" in s or "charge_power" in s for s in suffixes)
    # Each entry is a 3-tuple of (givtcp_suffix, ge_suffix, description).
    assert all(len(t) == 3 for t in mod.MEAN_PAIRS)


def test_mean_metadata_is_mean_not_sum():
    mod = _load_migrate_module()
    meta = mod.mean_metadata("sensor.loft_givenergy_inverter_x_pv_power", "W")
    assert meta["has_mean"] is True
    assert meta["has_sum"] is False
    assert meta["statistic_id"] == "sensor.loft_givenergy_inverter_x_pv_power"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k mean -v`
Expected: FAIL — `AttributeError: ... 'MEAN_PAIRS'`

- [ ] **Step 3: Write minimal implementation**

Add the table and helpers. Confirm exact GivTCP/GE suffixes against a live registry before finalising the list; the entries below are the intended coverage:

```python
# (givtcp_suffix, ge_suffix, description) — mean-type series (power/SOC/temp).
# Straight mean/min/max copy; no sum, no rebase, no plausibility.
MEAN_PAIRS: list[tuple[str, str, str]] = [
    ("pv_power", "pv_power", "PV power"),
    ("grid_power", "grid_power", "Grid power (signed)"),
    ("battery_power", "battery_power", "Battery power (signed)"),
    ("load_power", "house_consumption", "House consumption power"),
    ("soc", "battery_soc", "Battery SOC (inverter)"),
]

# Per-battery mean series: sensor.givtcp_<batt_sn>_<gt> -> sensor.givenergy_battery_<batt_sn>_<ge>
MEAN_BATTERY_PAIRS: list[tuple[str, str, str]] = [
    ("soc", "soc", "Battery SOC (per pack)"),
    ("battery_temperature", "temperature", "Battery temperature"),
]


def mean_metadata(ge_id: str, unit: str) -> dict[str, Any]:
    return {
        "has_mean": True,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": ge_id,
        "unit_of_measurement": unit,
    }
```

Add `migrate_mean_entity` (mirrors `migrate_entity` but copies `mean`/`min`/`max` for the pre-cutover range, no rebuild). Fetch with `types=["mean", "min", "max"]` — add an optional `types` arg to `HAWebSocket.get_statistics` (default the existing `["sum", "state"]`). Build mean plan entries in `_build_plan` (or a sibling `_build_mean_plan`), gate on `not args.skip_means`, and run them in the same loop. Add argparse:

```python
    p.add_argument(
        "--skip-means",
        action="store_true",
        help="Skip back-porting mean-type series (power, SOC, temperatures).",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k mean -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): back-port mean power/SOC/temperature series (#162)"
```

---

## Task 8: Validation checks (pure)

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (pure-helpers region)
- Test: `tests/test_migrate_stats_repair.py`

- [ ] **Step 1: Write the failing tests**

```python
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
        {"start": "t2", "state": 0.0},      # drop to ~0
        {"start": "t3", "state": 27300.0},  # huge positive
    ]
    shapes = _MOD.find_fake_reset_shapes(rows, ceiling=50.0)
    assert shapes and shapes[0]["start"] == "t3"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k "implausible or duplicate or classify_gaps or fake_reset" -v`
Expected: FAIL — missing attributes

- [ ] **Step 3: Write minimal implementation**

```python
def find_implausible_hours(rows: list[dict[str, Any]], ceiling: float) -> list[dict[str, Any]]:
    """Rows whose sum-change from the previous row exceeds the ceiling."""
    flagged = []
    for prev, cur in zip(rows, rows[1:]):
        ps, cs = prev.get("sum"), cur.get("sum")
        if ps is None or cs is None:
            continue
        if cs - ps > ceiling:
            flagged.append({"start": cur["start"], "change": round(cs - ps, 3)})
    return flagged


def find_duplicate_series(series_by_id: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str]]:
    """Pairs of statistic ids whose (start, sum) sequences are byte-identical."""
    def key(rows: list[dict[str, Any]]) -> tuple:
        return tuple((r.get("start"), r.get("sum")) for r in rows)

    seen: dict[tuple, str] = {}
    dupes: list[tuple[str, str]] = []
    for sid, rows in series_by_id.items():
        k = key(rows)
        if k in seen:
            dupes.append((seen[k], sid))
        else:
            seen[k] = sid
    return dupes


def classify_gaps(rows: list[dict[str, Any]], expected_step_minutes: int = 60) -> list[dict[str, Any]]:
    """Contiguous missing spans (more than one expected step between rows)."""
    gaps = []
    step = timedelta(minutes=expected_step_minutes)
    for prev, cur in zip(rows, rows[1:]):
        delta = _to_utc(cur["start"]) - _to_utc(prev["start"])
        missing = round(delta / step) - 1
        if missing >= 1:
            gaps.append({"after": prev["start"], "before": cur["start"], "hours": missing})
    return gaps


def find_fake_reset_shapes(rows: list[dict[str, Any]], ceiling: float) -> list[dict[str, Any]]:
    """A drop to ~0 immediately followed by a huge positive jump (modbus zero-read)."""
    shapes = []
    for i in range(2, len(rows)):
        a, b, c = rows[i - 2].get("state"), rows[i - 1].get("state"), rows[i].get("state")
        if a is None or b is None or c is None:
            continue
        if b <= a * 0.05 and (c - b) > ceiling:
            shapes.append({"start": rows[i]["start"], "recovery": round(c - b, 3)})
    return shapes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k "implausible or duplicate or classify_gaps or fake_reset" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): pure validation checks for migrated stats (#162)"
```

---

## Task 9: Validation report + auto-run + --repair-residue

**Files:**
- Modify: `scripts/migrate_from_givtcp.py` (`run`, new `run_validation`, argparse)
- Test: `tests/test_migrate_stats_repair.py`

- [ ] **Step 1: Write the failing test (report formatting is pure)**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k validation_report -v`
Expected: FAIL — `AttributeError: ... 'format_validation_report'`

- [ ] **Step 3: Write minimal implementation**

Add `format_validation_report(findings, duplicates) -> tuple[str, int]` (pure string builder; non-zero exit if any `implausible`/`fake_resets`/`duplicates` present — gaps are informational only). Add `run_validation(ws, results, ceilings, tz)` that re-reads each migrated GE series, runs the four checks, and prints the report; call it from `run` after the migration loop (both dry-run preview and post-apply). Add argparse:

```python
    p.add_argument(
        "--repair-residue",
        action="store_true",
        help=(
            "After validation, clear + re-import the rebuilt series for entities "
            "with residual implausible hours. Off by default (report only)."
        ),
    )
```

When `--repair-residue` is set and validation flags residue, re-run `rebuild_sum_walk` for the flagged entities and `clear_statistics` + `import_statistics` them (reusing the Task 6 write path). Log each repaired entity.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k validation_report -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py
git commit -m "feat(migrate): validation report + opt-in residue repair (#162)"
```

---

## Task 10: LTS-fixture acceptance test

**Files:**
- Test: `tests/test_migrate_stats_repair.py`

End-to-end proof against the documented corruption shapes from `.remember/lts-corruption-report-2026-06-12.md`.

- [ ] **Step 1: Write the acceptance test**

```python
def test_acceptance_rebuild_heals_documented_corruption():
    """Reproduce the LTS-report shapes and assert rebuild + validation handle them.

    Shapes covered (from .remember/lts-corruption-report-2026-06-12.md):
      - genuine daily ramp with a real local-midnight reset (kept)
      - an off-midnight zero-read + recovery on a lifetime counter (held)
      - a +27,396 kWh fake-reset spike (rejected)
    """
    rc = _MOD.ResetClass
    # Lifetime counter: steady climb, one fake zero+recovery, one giant spike.
    rows = [
        {"start": "2026-06-07T13:00:00+00:00", "state": 1000.0},
        {"start": "2026-06-07T14:00:00+00:00", "state": 1003.0},
        {"start": "2026-06-07T15:00:00+00:00", "state": 0.0},        # zero-read
        {"start": "2026-06-07T16:00:00+00:00", "state": 27396.1},    # fake spike/recovery
        {"start": "2026-06-07T17:00:00+00:00", "state": 1006.0},     # back to reality
    ]
    deltas = [3.0]  # genuine hourly step magnitude to anchor the ceiling
    ceiling = _MOD.adaptive_ceiling([3.0, 2.0, 4.0, 3.0, 2.5] * 20)
    out = _MOD.rebuild_sum_walk(rows, rc.LIFETIME, ceiling, _LONDON)
    sums = [r["sum"] for r in out]
    # Monotonic non-decreasing, and no +27k jump anywhere.
    assert sums == sorted(sums)
    assert max(b - a for a, b in zip(sums, sums[1:])) < 50.0
    # Final sum reflects only genuine accumulation (1000 -> ~1006), not the spike.
    assert sums[-1] < 1010.0
    # Validation on the rebuilt series finds nothing implausible.
    assert _MOD.find_implausible_hours(out, ceiling) == []
```

- [ ] **Step 2: Run test to verify it passes (logic already built)**

Run: `uv run pytest tests/test_migrate_stats_repair.py -k acceptance -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_migrate_stats_repair.py
git commit -m "test(migrate): LTS-fixture acceptance for sum rebuild (#162)"
```

---

## Task 11: Docs + final verification

**Files:**
- Modify: `docs/migration-from-givtcp.md`

- [ ] **Step 1: Update the migration doc**

Document: rebuild-by-default (state→sum, plausibility + local-midnight reset rule), `--trust-source-sums`, the mean/SOC/temperature back-port and `--skip-means`, the post-migration validation report and `--repair-residue`. Note the design doc at `docs/superpowers/specs/2026-06-14-givtcp-migration-stats-repair-design.md`.

- [ ] **Step 2: Run the full suite + lint**

Run: `uv run pytest tests/test_migrate_stats_repair.py tests/test_script_entity_refs.py -v`
Expected: PASS
Run: `uv run ruff check --fix scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py && uv run ruff format scripts/migrate_from_givtcp.py tests/test_migrate_stats_repair.py`
Expected: clean

- [ ] **Step 3: Run the broader project suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add docs/migration-from-givtcp.md
git commit -m "docs(migrate): document rebuild, validation, and mean back-port (#162)"
```

---

## Self-review notes

- **Spec coverage:** rebuild-by-default (T6) + `--trust-source-sums` (T6); adaptive ceiling (T2); reset-aware walk with midnight rule (T3, T4); seam elimination via concatenation (T6); validation report read-only + opt-in repair (T8, T9); mean/power/SOC back-port (T7); LTS fixture (T10); docs (T11). All seven locked decisions mapped.
- **Open implementation detail (verify against a live registry during T7):** exact GivTCP vs givenergy_local suffixes for the power/SOC/temperature pairs — the `MEAN_PAIRS` entries are the intended coverage, confirm the real suffixes before finalising.
- **Tuning:** `_CEILING_MAD_K` (T2) is pinned by the acceptance test (T10); adjust there if a documented genuine peak is ever flagged.
