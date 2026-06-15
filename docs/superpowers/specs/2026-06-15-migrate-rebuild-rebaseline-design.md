# GivTCP migration: recover from sustained shifts (fix the rebuild flat-line)

**Issue:** [#172](https://github.com/dewet22/givenergy-hass/issues/172)
**Date:** 2026-06-15 (rev 2 — incorporates Codex review round 2)
**Status:** design under review (Codex re-review pending)
**Touches:** `scripts/migrate_from_givtcp.py`, `tests/test_migrate_stats_repair.py`

## Problem

A live `--apply` silently flattened lifetime energy counters. `rebuild_sum_walk`
holds last-good on an over-ceiling delta and does not advance `prev_state` —
correct for a *transient* spike. GivTCP's lifetime PV counter had a one-time
~8,000 kWh artifact jump (a meter rebase/rollover, not real energy); the walk
rejected that single step, then `prev_state` stuck at the pre-jump value
forever, so every later (genuine, higher) reading was also rejected →
**permanent flat-line** (`pv_generation_total` pinned at 18958.5 while the
GivTCP source climbed past 27332). Recovered from a `mysqldump`. No gate caught
it: the dry-run validation reads the *existing* series (not the rebuild), and
the checks only flag *large* changes, never flatness/under-counting.

This rev folds in Codex's round-2 review (see Provenance). The earlier rev's
re-baseline was too weak (monotonic-≥ only, one-directional, discarded genuine
in-segment movement, left a state discontinuity, and validated/warned only
*after* the destructive write).

## Decisions

1. **Gate before the destructive write.** Build the full rebuilt candidate
   in-memory, run the *same* validation used in dry-run against it, and **refuse**
   the entity (and surface for the whole apply) *before* `clear_statistics` if it
   contains unexplained re-baselines, source-vs-rebuilt movement divergence, or
   source-moves-but-rebuilt-flat spans. Post-apply validation only confirms the
   write matches the approved candidate.
2. **Confirm a coherent new segment, not just `held ≥ prev_state`.** Buffer the
   held readings; re-baseline only when they form a coherent segment — adjacent
   held deltas mutually plausible within the time-scaled bound — in **either
   direction** (up or down rebase). Reject a candidate whose held readings
   themselves jump implausibly (three corrupt highs are not a segment).
3. **Book in-segment movement; suppress only the one-time offset.** On
   confirmation, add the buffered segment's internal plausible deltas to the
   running sum and suppress only the single offset (the artifact jump from the
   old level to the new segment). Never discard genuine accumulation.
4. **Normalized, continuous output state.** Maintain a segment offset so the
   emitted `state` timeline is continuous (no artifact discontinuity), consistent
   with held rows already being rewritten to last-good. The clean cumulative is
   the output; raw source discontinuities are not re-introduced.
5. **Time-scaled, reset-aware acceptance + smear.** A delta is plausible when
   `≤ ceiling × elapsed_hours`. A genuine multi-hour (gap) delta is **smeared**
   so each stored period is physically plausible — booking it on the resume hour
   would store an impossible single-hour spike that our own plausibility check
   would (rightly) flag. Smearing is **reset-aware**: `LIFETIME` gaps smear
   across days; `DAILY` gaps are **split at each local midnight** so each day's
   share is reset-correct; an `ANNUAL` (year-end-crossing) gap is **flagged /
   refused** (a cheap guard — see Scope note).
6. **Reset-aware aligned-movement source comparison.** Compare source vs rebuilt
   *movement* over the same pre-cutover window (reset-aware), not final-state vs
   final-sum (baselines and DAILY/ANNUAL resets make that incomparable).
   Separately verify the post-cutover segment preserves the GE movement.

### Scope note (from the live data)

The four identified outages (23h/69h/136h/532h, all 2025) **all cross day-ends**
but **none cross year-end**, and the only `ANNUAL` counter
(`battery_discharge_this_year`) is **not migrated**. So `DAILY` midnight-crossing
is the real, ubiquitous case (engineer it properly); `ANNUAL` year-end-crossing
gets only a conservative flag/refuse guard for the general OSS case.

## Core algorithm — `rebuild_sum_walk` (buffered segments)

State: `running` (clean cumulative sum), `prev_state` (last trusted raw state),
`prev_start`, and a `held` buffer (list of `(start, state)` for rows currently
being held pending a transient-vs-sustained decision).

Per row with numeric `state` (gap rows with `state is None` carry `running`
forward, unchanged):

- `elapsed = max(1, hours(prev_start → start))`; `bound = ceiling × elapsed`;
  `delta = state − prev_state`.
- **Accept** (`0 ≤ delta ≤ bound`): genuine accumulation. If `elapsed > 1`,
  **smear** (reset-aware per Decision 5) across the gap; else add `delta`. Flush
  any `held` buffer first as a transient (see below). Advance
  `prev_state`/`prev_start`; emit normalized state.
- **Boundary reset** (`delta < 0` at the counter's natural boundary): existing
  behaviour (`running += state`); flush held as transient; advance.
- **Otherwise** (over-bound, or off-boundary drop): append `(start, state)` to
  `held`, emit last-good (held) sum **and** state. Then decide:
  - **Transient** — the *next* accepted reading reverts toward `prev_state`
    (within `bound`): the held run was a transient spike; discard it (already
    emitted as held last-good), resume normally.
  - **Sustained segment** — `held` reaches the confirmation length **and** its
    adjacent internal deltas are each within the time-scaled bound (a coherent
    segment, any direction): re-baseline. Suppress the one-time offset
    (`held[0].state − prev_state`, add nothing), then **book the internal deltas**
    across the held rows (retroactively correct their emitted sums/states),
    advance `prev_state` to `held[-1].state`, record a re-baseline event, clear
    the buffer.
  - **Incoherent** — held readings jump implausibly among themselves: do **not**
    re-baseline; keep holding (these are corruption, not a segment), and record
    for validation.

Confirmation length and the segment-coherence test are module constants/helpers,
unit-tested independently.

## Validation (gate-before-write)

Pure checks, run on the in-memory candidate **before** any write:

- `find_flat_line_spans(rows, min_hours)` — sustained near-zero `sum`-delta spans
  (under-count signature).
- `compare_source_movement(givtcp_rows, rebuilt_rows, cutover, reset_class, tol)`
  — reset-aware aligned movement over the pre-cutover window; flags divergence
  beyond tolerance; separately checks the post-cutover segment preserves GE
  movement.
- Re-baseline events and smear events surfaced for transparency.

`migrate_entity` (apply path) refuses the entity before `clear_statistics` when
the candidate has: a source-moves-but-rebuilt-flat span, source-movement
divergence beyond tolerance, an incoherent held run, or a reset-crossing
`ANNUAL` gap. `format_validation_report` renders all finding types. Post-apply
validation re-reads and asserts the stored series matches the approved
candidate.

## Dry-run

`MigrationResult` carries the rebuilt candidate (`rebuilt_rows`) and the aligned
source rows needed for `compare_source_movement`. In dry-run (`applied=False`),
validation analyses the **candidate**, not the current HA series (header:
"dry-run: rebuilt preview"). This is the same validation the apply gate runs, so
the preview is faithful to what `--apply` would do.

## Testing

`rebuild_sum_walk` (pure), covering Codex's expanded fixture:

- **Sustained upward rebase** (the live 8 MWh artifact then genuine climb):
  recovers, not flat; offset suppressed; in-segment deltas booked; totals track
  the genuine source.
- **Sustained downward rebase** (meter reset/rollover to a lower level):
  recovers symmetrically.
- **Three corrupt highs then recovery to the real lower value**: not mistaken
  for a segment; does not get permanently stuck.
- **Confirmed segment with internal increments**: the genuine in-segment deltas
  (`+2.7, +2.4 …`) are retained, not discarded.
- **Transient spike**: still held (no re-baseline).
- **Gaps crossing / not crossing a DAILY reset**: reset-aware smear splits at
  local midnight; each day's share is reset-correct; a non-crossing LIFETIME gap
  smears cleanly; an ANNUAL year-end-crossing gap is flagged/refused.
- **Output state is continuous** across holds and re-baselines (no discontinuity).

Pure validation helpers (`find_flat_line_spans`, `compare_source_movement`) and
the apply-gate refusal path (candidate with a flat span / divergence is refused
before any write) are unit-tested. Dry-run validates the candidate even when the
mocked current HA series is healthy.

## Verification (post-merge, against live data, backed up)

Re-run the live dry-run (validating the candidate) → expect no flat-lines /
divergence and explained re-baselines/smears; then a backed-up `--apply` →
confirm lifetime counters climb and reconcile with the GivTCP source movement,
and the four known day-crossing gaps smear per-day rather than spiking.

## Provenance

- **Ours / the live failure:** the diagnosis, time-scaled `ceiling × elapsed`
  plausibility, and reset-aware **daily smear** (kept over Codex's "drop smear":
  booking a multi-day gap on the resume hour stores a physically-impossible
  single-hour spike — *less* faithful than a plausible smear, and it would fail
  our own plausibility check).
- **CodeRabbit (#172 plan):** count-based hold tracking, flat-line span
  detection, source-comparison and dry-run-validates-rebuilt direction.
- **Codex (round 2):** gate-before-write; coherent-segment (bidirectional)
  confirmation rather than monotonic-≥; book in-segment movement / suppress only
  the offset; normalized continuous output state; reset-aware aligned-movement
  source comparison; reset-aware handling of gaps. We diverge from Codex only on
  *whether* to smear (we smear, reset-aware) — see the smear rationale above.

## Out of scope

- Diurnal-shaped smear (per-sensor time-of-day weighting) — daily-uniform is
  enough for the Energy dashboard.
- The mean/SOC back-port (#169) — independent; does not use `rebuild_sum_walk`.
