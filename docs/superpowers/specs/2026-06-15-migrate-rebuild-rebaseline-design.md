# GivTCP migration: recover from sustained shifts (fix the rebuild flat-line)

**Issue:** [#172](https://github.com/dewet22/givenergy-hass/issues/172)
**Date:** 2026-06-15 (rev 4 — incorporates Codex review rounds 2–4)
**Status:** design cleared for implementation plan (Codex round 4: no further design concerns)
**Touches:** `scripts/migrate_from_givtcp.py`, `tests/test_migrate_stats_repair.py`

## Problem

A live `--apply` silently flattened lifetime energy counters. `rebuild_sum_walk`
holds last-good on an over-ceiling delta and does not advance `prev_state` —
correct for a *transient* spike. GivTCP's lifetime PV counter had a one-time
~8,000 kWh artifact jump (meter rebase, not real energy); the walk rejected that
step, then `prev_state` stuck forever, so every later genuine reading was also
rejected → **permanent flat-line** (`pv_generation_total` pinned at 18958.5
while the source climbed past 27332). Recovered from a `mysqldump`. No gate
caught it: dry-run validation read the *existing* series (not the rebuild), and
the checks only flag *large* changes, never flatness/under-counting.

## Decisions

1. **Two-phase, whole-run gate (validate-all-before-any-write).** Phase A: build
   and validate **every** candidate in-memory (no writes). Phase B: only if the
   entire plan passes, perform `clear_statistics` + `import_statistics` for all
   entities. This stops *validation*-driven partial writes, but Phase B is **not
   transactional** — a recorder/API failure on entity N can still leave 1..N-1
   rewritten and N cleared. So on any Phase-B error: abort immediately, report
   exactly which entities were written/cleared, and point to the **mandatory
   pre-apply backup** (the `mysqldump` of the statistics tables) for deterministic
   recovery — restore is the proven net; there is no auto-rollback. Post-apply
   validation re-reads and confirms each stored series matches its approved
   candidate.
2. **Confirm a coherent segment; offset bidirectional, internals non-negative.**
   Buffer held readings; re-baseline only when they form a coherent cumulative
   segment: the initial **offset** (old level → new segment) may be either
   direction (up rebase / down reset), but the segment's **internal** adjacent
   deltas must each be `0 ≤ d ≤ bound` (monotonic like a real counter). Oscillating
   or implausibly-jumping held readings are **not** a segment.
3. **Book in-segment movement; suppress only the one-time offset.** On
   confirmation, add the segment's internal deltas to `running` and suppress only
   the single offset. Never discard genuine accumulation.
4. **Normalized, continuous output state** via a maintained segment offset — the
   emitted `state` timeline has no artifact discontinuity (consistent with held
   rows already rewritten to last-good).
5. **Time-scaled, reset-aware acceptance + smear.** A delta is plausible when
   `≤ ceiling × elapsed_hours`. A genuine multi-hour delta with **no reset inside
   the gap** is **smeared** (each stored period physically plausible — booking it
   on the resume hour would store an impossible single-hour spike our own check
   would flag). A gap that **crosses a reset** (DAILY local-midnight, ANNUAL
   year-end) is **not reconstructable from endpoints** (the reset destroyed the
   pre-reset accumulation) and is **not smeared**. Per the maintainer's choice,
   such a gap is migrated by **carrying the cumulative flat across it** and
   emitting a loud, explicit `gap_undercount` event (knowingly under-counts the
   gap rather than leave the entity unfixed or fabricate data). If real
   intermediate rows exist, they reconstruct each segment normally.
6. **Reset-aware aligned-movement source comparison, offset-excluded.** Compare
   source vs rebuilt *movement* over the same pre-cutover window (reset-aware),
   against the **cleaned expected** movement = accepted + booked-in-segment deltas
   (i.e. raw source movement **minus recorded one-time rebase offsets**) — else
   the correctly-fixed candidate looks divergent and gets blocked. Separately
   verify the post-cutover segment preserves GE movement.
7. **Unresolved held buffer refuses.** A non-empty held buffer at walk completion
   (a coherent-but-too-short run at EOF/cutover, or an undecidable tail) is
   neither confirmed nor proven-corrupt — it silently flattens the tail below the
   flat-span/divergence thresholds. Record it as an unresolved finding and refuse.

### Failure classes (gate behaviour)

- **Accepted (warn, proceed):** a recorded `gap_undercount` event (known DAILY/
  ANNUAL reset-crossing gap) and recorded re-baseline/smear events — surfaced
  loudly but not blocking.
- **Abort the whole run:** an *unexplained* flat-line span (not at a recorded
  gap), source-movement divergence beyond tolerance, an unresolved held buffer,
  or an incoherent held run. Any one fails Phase A → no writes occur.

### Scope note (live data)

The four outages (23h/69h/136h/532h, all 2025) **all cross day-ends, none cross
year-end**, and the only ANNUAL counter (`battery_discharge_this_year`) is not
migrated. So `_total` (LIFETIME) counters reconstruct + smear cleanly; `_today`
(DAILY) counters hit the carry-flat-+-flag path across each outage. ANNUAL gets
the same treatment for the general OSS case (won't fire here).

## Core algorithm — `rebuild_sum_walk` (buffered segments)

State: `running` (clean cumulative), `prev_state` (last trusted raw state),
`prev_start`, `held` buffer of `(start, state)`. Gap rows (`state is None`) carry
`running` forward.

Per row with numeric `state`: `elapsed = max(1, hours(prev_start→start))`,
`bound = ceiling × elapsed`, `delta = state − prev_state`.

- **Reset-crossing gap (checked FIRST, before any delta-sign branch)** — if
  `elapsed > 1` and a DAILY/ANNUAL reset boundary lies within `(prev_start,
  start)` with no intermediate rows: not reconstructable from endpoints. Carry
  `running` flat, record a `gap_undercount` event, advance
  `prev_state`/`prev_start` to the resume `state` — **regardless of the endpoint
  delta's sign** (a DAILY midnight gap is typically a *negative* delta, so it must
  not fall through to `held`/segment confirmation as a downward rebase). Flush
  any held buffer as transient first.
- **Accept** (`0 ≤ delta ≤ bound`): genuine accumulation; flush any held buffer
  as transient first. If `elapsed > 1` (a gap that does **not** cross a reset) →
  **smear** across the gap days; else add `delta`. Advance; emit normalized state.
- **Boundary reset** (`delta < 0` at the natural boundary): `running += state`;
  flush held as transient; advance.
- **Otherwise** (over-bound / off-boundary drop): append to `held`, emit last-good
  sum **and** state, then decide:
  - **Transient** — next accepted reading reverts toward `prev_state`: discard
    held (already emitted last-good); resume.
  - **Coherent segment** — `held` reaches confirmation length and its internal
    adjacent deltas are each `0 ≤ d ≤ bound` (offset to `held[0]` may be either
    direction): re-baseline — suppress the offset, **book** the internal deltas
    (retroactively correct the held rows' sums/states), advance `prev_state` to
    `held[-1]`, record a re-baseline event, clear buffer.
  - **Incoherent** — held readings jump implausibly among themselves: keep
    holding; record for validation (does not re-baseline).
- **At completion:** a non-empty `held` buffer → record an unresolved finding.

Confirmation length and the coherence test are module constants/helpers,
unit-tested.

## Validation (Phase A, pre-write)

Pure checks on the in-memory candidate:

- `find_flat_line_spans(rows, min_hours)` — sustained near-zero `sum`-delta spans,
  **excluding** spans covered by a recorded `gap_undercount` event.
- `compare_source_movement(...)` — reset-aware aligned movement over the
  pre-cutover window vs the **cleaned expected** movement (offsets excluded);
  flags divergence beyond tolerance; checks post-cutover preserves GE movement.
- Surfaces re-baseline / smear / gap_undercount / unresolved findings.

`format_validation_report` renders all finding types and marks each
accepted-vs-blocking. The Phase-A driver aborts the whole apply if any candidate
has a blocking finding.

## Dry-run

`MigrationResult` carries the rebuilt candidate (`rebuilt_rows`) and the aligned
source rows + recorded offset events needed for `compare_source_movement`. In
dry-run, validation analyses the **candidate** (header "dry-run: rebuilt
preview") — the same validation Phase A runs — so the preview is faithful.

## Testing (pure `rebuild_sum_walk` + helpers)

Covering Codex's expanded fixture and round-3 cases:

- Sustained **upward** rebase (the live 8 MWh artifact then genuine climb):
  recovers, not flat; offset suppressed; in-segment deltas booked; totals track
  cleaned source movement.
- Sustained **downward** rebase (meter reset to lower level): recovers.
- **Three corrupt highs then recovery to the real lower value**: not a segment;
  does not get stuck.
- **Oscillating corrupt readings** (`abs(delta) ≤ bound` but not monotonic): not
  a segment (internal deltas must be non-negative).
- **Confirmed segment internal increments retained** (`+2.7, +2.4` booked).
- **Transient spike**: held, no re-baseline.
- **LIFETIME gap (no reset)**: smeared per day; totals preserved.
- **DAILY gap crossing midnight, negative endpoint delta** (`23:00=8` →
  `02:00=2`): classified as reset-crossing *before* the delta-sign branch →
  `gap_undercount` (carry-flat, accepted/warn), **not** misread as a downward
  rebase; not smeared; not aborting.
- **Unresolved held tail at EOF/cutover**: recorded → refused.
- **Output state continuous** across holds and re-baselines.
- Validation: unexplained flat span / divergence / unresolved held → Phase-A
  abort (no writes); recorded gap_undercount → warn, proceed. Whole-run gate:
  one blocking candidate aborts all. Dry-run validates the candidate even when
  the mocked current HA series is healthy.
- **Mid-Phase-B failure** (simulated import error on the Nth entity): aborts
  immediately, reports which entities were written/cleared, points to the backup,
  and does not continue writing.

## Verification (post-merge, backed up, live)

Dry-run validating the candidate → expect explained re-baselines/smears, DAILY
gap_undercount flags, no unexplained flat/divergence; then a backed-up `--apply`
→ `_total` counters climb and reconcile with cleaned source movement; `_today`
counters carry flat across the four day-crossing gaps with flags; nothing else
flattened.

## Provenance

- **Ours / live failure:** the diagnosis, time-scaled `ceiling × elapsed`
  plausibility, reset-aware smear (kept over "drop smear" — endpoint booking
  stores a physically-impossible spike that fails our own check), and the
  carry-flat-+-flag choice for un-reconstructable DAILY gaps (maintainer's call:
  under-count beats leaving the rest unfixed).
- **CodeRabbit:** count-based hold tracking, flat-line detection, source-
  comparison + dry-run-validates-rebuilt direction.
- **Codex (rounds 2–4):** gate-before-write → two-phase whole-run gate; coherent
  bidirectional-offset / non-negative-internal segment confirmation; book
  in-segment / suppress offset; normalized state; offset-excluded reset-aware
  movement comparison; DAILY-crossing un-reconstructability; unresolved-held
  refusal. Round 4: classify reset-crossing gaps *before* the delta-sign branch;
  Phase B documented as validate-all-before-any-write (non-transactional) with
  backup-based recovery + a simulated mid-Phase-B-failure test.

## Out of scope

- Diurnal-shaped smear — daily-uniform suffices for the Energy dashboard.
- Cross-entity inference (deriving a DAILY counter's gap from its LIFETIME
  sibling) — not attempted; DAILY reset-crossing gaps carry-flat + flag instead.
- The mean/SOC back-port (#169) — independent; doesn't use `rebuild_sum_walk`.
