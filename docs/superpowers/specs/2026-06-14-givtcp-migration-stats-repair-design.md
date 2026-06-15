# GivTCP migration: sum-rebuild, validation, and mean back-port

**Issue:** [#162](https://github.com/dewet22/givenergy-hass/issues/162) — *migrate_from_givtcp: re-base sum statistics at the join, optionally rebuild implausible source sums*
**Date:** 2026-06-14
**Status:** design approved, pending implementation plan
**Touches:** `scripts/migrate_from_givtcp.py`, `docs/migration-from-givtcp.md`, `tests/`

## Problem

`migrate_from_givtcp.py` migrates GivTCP long-term statistics (LTS) into the
`givenergy_local` entities. `mean`/`min`/`max` series migrate cleanly, but the
`sum` column — the one that drives every energy chart — breaks two ways:

1. **Join seam.** Back-ported GivTCP rows carry GivTCP's cumulative sum while the
   integration's own rows continue from a near-zero baseline. The hour where they
   meet records a single huge negative `change`, wrecking anything summing across
   it. A 430-day scan of the maintainer install found the seam hit **eleven
   entities** at differing magnitudes, with one firing five days earlier — so a
   single join timestamp cannot be assumed.
2. **Broken source sums copied verbatim.** GivTCP's own `sum` column was
   structurally broken on this install (negative months; and in the back-filled
   range every local-midnight hour carries a negative `change` equal to the prior
   day's accumulation — the back-filled sum was simply set equal to `state`,
   ~80–90 affected days per daily counter). Copying it verbatim propagates the
   corruption.

Both `state` and `sum` are suspect on real data, so rebuild cannot blindly trust
either. Separately, the migration only carries energy-counter (`sum`) statistics,
so power/SOC/temperature **mean** series start at the integration's install date —
hour-of-day heatmaps show ~6 weeks instead of the full year that exists in
GivTCP's history.

The acceptance fixture for all of this is the classified worklist at
`.remember/lts-corruption-report-2026-06-12.md` (every bad hour with timestamp
and magnitude).

## Decisions (locked during brainstorming)

1. **Scope:** one spec covering sum-rebuild, validation, and mean back-port.
2. **Rebuild basis:** `state` is also suspect, so the rebuild walk is
   plausibility-guarded, not a blind state→sum recompute.
3. **Plausibility bound:** adaptive, derived from each entity's own series — no
   hard-coded per-install ceilings (this is an OSS tool run on varied hardware).
4. **Validation role:** read-only report by default, with opt-in targeted repair.
5. **Rebuild default:** rebuild is the default behaviour; `--trust-source-sums`
   is the escape hatch to the old verbatim-copy + rebase path.
6. **Mean back-port:** full power family **plus** other mean series (battery SOC
   at both inverter and per-battery level, temperatures).
7. **Rebuild walk:** Approach A (two-pass adaptive walk), hardened with the domain
   rule that **resets only legitimately occur at a counter's natural boundary**
   (daily counters at local midnight; annual counters at the year boundary;
   lifetime counters never).

## Architecture & CLI surface

The script remains a single-file CLI but separates a **pure repair core**
(entity classification, adaptive ceiling, the rebuild walk, validation checks,
mapping) from the WebSocket I/O, so the core is unit-testable without HA.

CLI changes:

- **Sum rebuild is the default.** `--trust-source-sums` restores the current
  verbatim-copy + `rebase_sum` behaviour for installs with known-good GivTCP sums.
- **Mean/power migration on by default** (purely additive history, no
  sum-continuity risk); `--skip-means` to opt out.
- **Validation runs automatically after migration as a read-only report.**
  `--repair-residue` opts into targeted fixes for anything rebuild didn't cover.
- Unchanged: dry-run default, `--apply` to write, cut-over auto-detect when
  `--cutover` omitted, `--include-charge-from-grid`.

## Sum-rebuild (the core)

**Structural shift:** in rebuild mode we no longer "copy GivTCP sums then rebase
GE". We **concatenate the `state` timeline across the cut-over** (GivTCP before
the boundary, `givenergy_local` from the boundary) and walk it **once** to
produce a single continuous `sum`. This structurally eliminates the join seam (no
rebase needed) and cleans GE-side fake-reset residue in the same pass.
`rebase_sum` survives only on the `--trust-source-sums` path.

### Entity classification (by suffix)

- `DAILY` — `_today` sensors: reset to 0 at **local midnight**.
- `ANNUAL` — `_this_year` sensors: reset at the **year boundary**.
- `LIFETIME` — `_total` sensors: **never** reset within the migration window.

### Pass 1 — adaptive ceiling

From the entity's positive state-deltas, compute a robust per-hour ceiling that
is resistant to giant outliers (median + k·MAD, or a high percentile under a
sanity cap — computed from the distribution's lower/middle mass so a handful of
huge fakes cannot inflate it). The fakes are orders of magnitude over genuine
hourly steps; the ceiling only has to catch the subtle cases.

### Pass 2 — guarded walk over `state` (local time)

Walk chronologically; `Δ = state[i] − state[i−1]`:

- **Δ ∈ [0, ceiling]** → accept: `sum += Δ`.
- **Δ < 0 (decrease)** → a legitimate reset **only if** the counter's natural
  boundary applies: `DAILY` within ±window of local midnight, or `ANNUAL` near
  the year boundary. Then start a fresh segment: `sum += state[i]`. **Any other
  decrease — off-boundary, or any decrease on a `LIFETIME` counter — is
  corruption** → hold last-good (carry `sum`, add nothing).
- **Δ > ceiling** → fake spike / zero-read recovery → hold last-good (drop the
  bogus delta).
- **Missing hours (gaps)** → carry `sum` forward; never fabricate rows.

The ±midnight window is evaluated in **local time** (Europe/London, DST-aware —
resets appear at 23:00Z in BST, 00:00Z in GMT) with tolerance for inverter-clock
lag.

**Output:** a continuous `(state, sum)` series imported into the GE
`statistic_id`. Because the walk spans the cut-over, the seam never exists.

**Known risk:** the adaptive ceiling needs enough clean hours to anchor. On a
badly-corrupted series the estimator is computed from the lower/middle mass of
the delta distribution so giant fakes cannot skew it — this is the part to prove
hardest against the LTS fixture.

## Validation pass

Runs automatically after migration as a **read-only report** over the resulting
GE series:

- **Residual implausible hours** — Δsum above the adaptive ceiling. Empty after a
  good rebuild (proof it worked; safety net under `--trust-source-sums`).
- **Duplicate series** — two GE targets with byte-identical stats across a range
  (the one-GivTCP-series-mapped-to-two-targets case). Flag; do not repair twice.
- **Gaps vs deletions** — contiguous missing spans classified as outage-gaps (no
  surrounding negative step) vs suspicious deletions, reported informationally so
  a multi-day outage does not read as alarming.
- **Fake-reset shape** — a large positive Δ immediately following a drop to ~0,
  for any residue that slipped through.

Output: a table + summary, non-zero exit code if anything substantive is flagged.
`--repair-residue` (opt-in, off by default) applies a targeted `clear` +
re-import of the rebuilt series for **only** the flagged entities/spans — never a
blind sweep.

## Mean / power back-port

A new curated `MEAN_PAIRS` list alongside `INVERTER_PAIRS`:

- Power: PV power, grid power (import/export), battery charge/discharge power,
  load/consumption power.
- Other means: battery SOC (inverter-aggregate `battery_soc` **and** per-battery
  `soc`), temperatures.

For each: a **straight copy of the `mean`/`min`/`max` columns** from GivTCP
source to GE target over the pre-cut-over range — `has_mean=True, has_sum=False`,
no rebase, no plausibility (means do not accumulate, so none of the sum problems
apply). Same registry resolver; unmapped targets skipped per-entity as today. On
by default; `--skip-means` to opt out.

## Testing

- **Pure-function core** — `classify_entity`, `adaptive_ceiling`, `rebuild_walk`,
  and each validation check are pure and unit-tested in isolation; WS I/O stays a
  thin shell.
- **LTS fixture as acceptance test** — construct `state` series reproducing the
  documented bad hours from `.remember/lts-corruption-report-2026-06-12.md`: the
  11-entity seam, the ~80–90 midnight-negative back-fill days, the +27,000 kWh
  fake-reset spikes, the byte-identical duplicate pair. Assert: rebuild yields
  clean monotonic sums; the midnight rule accepts genuine daily resets while
  rejecting off-midnight drops; validation flags exactly the known-bad hours on
  un-repaired data and is clean after rebuild.
- **Regression** — existing `rebase_sum` / `_slugify` / entity-ref tests stay
  green; the `--trust-source-sums` path keeps the old behaviour under test.

## Out of scope

- Repairing the live `givenergy_local` recorder outside a migration run (the
  general LTS-repair tooling angle of #162 beyond what `--repair-residue`
  covers).
- The transient zero-read root cause itself (fixed in givenergy-modbus #255/#256;
  this tooling only cleans the residual shape it left in history).
