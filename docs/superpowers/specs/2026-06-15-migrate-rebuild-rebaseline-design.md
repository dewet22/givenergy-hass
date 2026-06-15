# GivTCP migration: recover from sustained shifts (fix the rebuild flat-line)

**Issue:** [#172](https://github.com/dewet22/givenergy-hass/issues/172) — `migrate_from_givtcp --apply` can silently flatten lifetime counters
**Date:** 2026-06-15
**Status:** design approved, pending implementation plan
**Touches:** `scripts/migrate_from_givtcp.py`, `tests/test_migrate_stats_repair.py`

## Problem

A live `--apply` flattened lifetime energy counters. `rebuild_sum_walk` holds the
last-good value on an over-ceiling delta and deliberately does **not** advance
`prev_state` — correct for a *transient* spike (so the recovery is measured
against the last trusted reading). But GivTCP's lifetime PV counter had a
one-time ~8,000 kWh artifact jump (a meter rebase/rollover, not real energy).
The walk correctly rejected that single step, but then `prev_state` stuck at the
pre-jump value forever, so every later (genuine, higher) reading was also
> ceiling vs the stale baseline and got held → **permanent flat-line**.

Evidence: migrated `pv_generation_total` pinned flat at `18958.5` while the
GivTCP source `invertor_energy_total_kwh` climbed past `27332` with genuine
+2–3 kWh/h deltas. Recovered from a `mysqldump` backup.

Nothing caught it because no gate checks the rebuilt output for *under*-counting:
the dry-run validation reads the **existing** HA series (not the rebuild), and
both validation passes only flag *large* changes — a flat sum has none.

This design was developed against the live failure and reconciled with
CodeRabbit's independent plan on #172 (see "Provenance" below).

## Decisions (locked during brainstorming)

1. **Detection of sustained vs transient** — CodeRabbit's count-based approach:
   after `K=3` consecutive holds whose held states are monotonically ≥
   `prev_state`, treat it as a sustained shift and re-baseline. (Robust against a
   2-reading transient, which a 1-step escrow would mis-classify.)
2. **Plausibility is time-scaled** — a delta is plausible when
   `≤ ceiling × elapsed_hours` (elapsed derived from row timestamps). This books
   legitimate accumulation across a real source gap instead of rejecting it (and
   fixes a latent under-count CodeRabbit's plan would have shipped by blanket-
   suppressing).
3. **Re-baseline suppresses the artifact gap** — anything reaching the K=3 path
   exceeded `ceiling × elapsed` (a true artifact), so re-baseline adds nothing to
   the running sum. Legitimate gaps never reach this path; they are accepted at
   the time-scaled test.
4. **Daily smear of booked gaps** — a multi-hour delta accepted via the
   time-scaled test is distributed across the gap as evenly-incrementing
   synthesised rows at daily granularity, so the cumulative climbs smoothly
   rather than spiking on the resume hour. The total is preserved; each smear is
   logged (the data is interpolated, so it must be transparent, not hidden).
5. **Dry-run validates the rebuilt output**, not the current HA series.
6. **No separate interim `--apply` guard** — this *is* the fix; `--apply` becomes
   safe on merge (it keeps the `--max-kw` requirement from #170).

## Core algorithm — `rebuild_sum_walk`

Walk normalised rows ascending, maintaining `running`, `prev_state`,
`prev_start`, `consecutive_holds`, and `held_states`.

For each row with a numeric `state`:

- `elapsed_hours = max(1, hours between prev_start and this start)`.
- `bound = ceiling * elapsed_hours` (when `ceiling` is not None).
- `delta = state - prev_state`.

Branches:

1. **Accept** — `0 ≤ delta ≤ bound`: this is genuine (possibly multi-hour)
   accumulation. If `elapsed_hours > 1`, **smear**: emit evenly-incrementing
   synthesised rows across the gap (daily granularity) summing to `delta`; else
   add `delta` to `running` on this row. Advance `prev_state`/`prev_start`; reset
   hold tracking.
2. **Boundary reset** — `delta < 0` at the counter's natural boundary
   (`_is_reset_boundary`): existing behaviour (`running += state`); reset hold
   tracking.
3. **Hold** — otherwise (over-bound spike, or off-boundary decrease): carry
   `running`, set the row's `state` to `prev_state` (hold state too — the #170
   fix), increment `consecutive_holds`, append `state` to `held_states`. Do
   **not** advance `prev_state`.
4. **Re-baseline** — once `consecutive_holds ≥ K (=3)` and all `held_states` are
   monotonically ≥ `prev_state`: the shift is sustained, not transient. Set
   `prev_state = state`, `prev_start = start`, add nothing to `running` (artifact
   suppressed), record a re-baseline event, reset hold tracking. Subsequent
   genuine deltas then accumulate from the new level.

Gap rows (`state is None`) carry `running` forward, untouched (unchanged).

`K` and the smear granularity are module constants.

## Smear helper

`_smear_gap(prev_sum, total_delta, start, end, prev_start)` → list of
synthesised rows with `sum` rising linearly from `prev_sum` to
`prev_sum + total_delta` across the gap at daily steps (hourly slots within a
day share the day's increment evenly). Pure and unit-tested. The walk splices
its output in place of the single jumping row.

## Validation additions

- `find_flat_line_spans(rows, min_hours=6)` — consecutive near-zero `sum`-delta
  spans; the under-count signature the current checks can't see. Returns
  `{start, hours}` entries.
- `compare_source_rebuilt(givtcp_rows, rebuilt_rows, tolerance_pct=5)` — compares
  the source's final `state`-span against the rebuilt `sum`-span; flags when they
  diverge beyond tolerance. Suppressed artifacts legitimately cause some
  divergence, so this is reported (and, at `--apply`, warned loudly) rather than
  fatal.
- `run_validation` records `flat_lines`, `rebaseline_events`, `smear_events`, and
  `source_comparison` per entity; `format_validation_report` renders each.

## Dry-run fix

- `MigrationResult` gains `rebuilt_rows: list[dict] | None` and
  `givtcp_final_state: float | None`, populated in `migrate_entity` after the
  rebuild.
- In `run_validation`, when `applied=False` and `rebuilt_rows` is present,
  validate **those** rows (header: "dry-run: rebuilt preview") instead of
  re-reading the current HA series. Source comparison uses `givtcp_final_state`.

## Testing

- `rebuild_sum_walk`: re-baseline after a sustained shift (not flat afterward);
  transient spike still held (no re-baseline); LIFETIME monotonic shift recovers
  at K=3; time-scaled accept books a legitimate post-gap delta; daily smear
  produces evenly-climbing synthesised rows summing to the gap; held rows carry
  last-good `state` (the #170 guarantee).
- **Live-failure fixture:** a normal climb → an ~8,000 kWh single-hour artifact
  jump → continued genuine +2–3 kWh/h climb. Assert the rebuilt series recovers
  (not flat), the artifact is suppressed, and the rebuilt total tracks the
  genuine source — the exact shape that flattened on the live `--apply`.
- `find_flat_line_spans` (flags ≥ min_hours, ignores shorter); `compare_source_rebuilt`
  (flags > tolerance, passes within); dry-run validates rebuilt output (flags a
  flat rebuilt series even when the mocked "current" HA series is healthy).
- Existing rebuild/validation/ceiling tests stay green.

## Verification (post-merge, against live data)

Re-run the live dry-run (now validating the rebuilt output) and confirm no
flat-lines / source divergence; then a backed-up `--apply` and confirm the
lifetime counters climb (match the GivTCP source) and totals reconcile.

## Provenance

Independently designed from the live failure and CodeRabbit's #172 plan. From
CodeRabbit: the `K=3` consecutive-hold + monotonic-≥ detector, flat-line span
detection, source-vs-rebuilt comparison, and the dry-run-validates-rebuilt fix.
Added here: **time-scaled plausibility (`ceiling × elapsed_hours`)** so genuine
post-gap accumulation is booked rather than suppressed (CodeRabbit's plan
explicitly accepted that under-count), and **daily smearing** of booked gaps for
natural-looking history.

## Out of scope

- Diurnal-shaped smear (per-sensor time-of-day profiles) — daily-uniform is
  enough for the Energy dashboard.
- The mean/SOC back-port (#169) — independent; does not use `rebuild_sum_walk`.
