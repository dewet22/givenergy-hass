# Mission Control dashboard — design

Brainstormed and approved 2026-06-12. Successor to the five-direction
exploration (`docs/design/dashboard-redesign-brief.md`): of the original five,
classic/flow/glance/analyst have shipped as strategy modes; Story and Coach
remained. This design merges their strongest ideas — narrative-of-the-day and
tariff-aware decision support — into one new mode rather than building them
separately, and adds two concepts that emerged in the brainstorm (Ledger,
Observatory).

## Decisions (locked during brainstorm)

- **Skeleton: hub + real tabs.** Mission Control is the entry view with
  compact summaries; Tape, Ledger and Observatory are real, bookmarkable
  Lovelace views — not overlays.
- **Mission Control layout: tape-centred.** The day tape is the spine of the
  view; the live power flow shrinks to a compact panel docked at the "now"
  cursor. (Flow-centred was the fallback if the docked mini-flow proves too
  cramped.)
- **Tape window: rolling −12h → +12h.** "Now" is always pinned centre; the
  tape scrolls beneath it.
- **Ledger depth: priced flows + one counterfactual.** Honest arithmetic
  (energy × rate) plus a single modelled claim — "what today would have cost
  with no battery/solar". No inferred line items ("peak avoidance +£1.12"
  style attribution was explicitly rejected: too easy to be confidently
  wrong).
- **Architecture: hybrid.** The £ numbers are Python sensors (recorded to
  long-term statistics, usable in automations); everything visual is computed
  client-side in cards.
- **Audience: the author's install first**, generalise afterwards. Desktop
  browser is the primary surface. Octopus tariff rates and a Solcast solar
  forecast are present.
- **Configuration is explicit.** Tariff/forecast entity ids are named in
  strategy options and config-entry options. No autodetection in v1.

## Views (`mode: mission`)

1. **Mission Control** (entry, `panel: true`)
   - Glance strip: SOC, PV now + today, today's net cost, health tick.
   - The tape (strip variant, see below).
   - Bottom tiles: Ledger summary -> Ledger tab, Observatory summary ->
     Observatory tab, next-action hint (v1: current rate + next band change
     heuristics only — no recommendations engine).
2. **Tape** (deep) — full-height tape: layer toggles, drag-to-scrub previous
   days.
3. **Ledger** (deep) — headline net cost; counterfactual comparison labelled
   *modelled*; import/export split by tariff band; month-to-date and
   last-30-days from LTS of the money sensors.
4. **Observatory** (deep) — `ge-cell-heatmap` centrepiece plus
   statistics-graph cards: pack voltage-spread trend (balance drift),
   cycle-count trajectory, temperature-spread history,
   charge-energy-per-cycle as a soft degradation proxy. Cards whose sensor
   lacks statistics are omitted.

Consistent with the other immersive modes, the mission views are followed by
the classic view set, and kiosk hints are emitted when `kiosk-mode` is
detected. Existing modes are untouched; EMS plants fall back to classic.

## The tape card (`custom:givenergy-tape`)

Rolling ±12h timeline, axis redrawn every minute. Layers (toggleable in the
deep tab, all on by default):

- **Tariff bands** — background tints (cheap/standard/peak) with p/kWh
  labels; past from rate history, future from the rate entity's forward-rates
  attribute (Octopus integration shape supported in v1).
- **Solar** — actual generation area (recorder history) up to now; forecast
  curve dashed beyond it, handing over at the cursor.
- **House consumption** — actual area behind solar.
- **SOC** — solid history; dashed projection ahead. Projection v1 is honest
  but simple: current SOC + scheduled charge/discharge slots + forecast PV +
  trailing-7-day hourly consumption baseline (LTS). Rendered unmistakably as
  a projection.
- **Plan blocks** — the inverter's charge/discharge slot windows as outlined
  blocks in the future half (real device state, not inference).
- **Events** — diamond markers derived client-side from threshold crossings
  in already-fetched history: charge window start/end, export began/stopped,
  SOC hit 100%/floor, grid import during a peak band.
- **Now cursor + mini-flow** — docked panel with live PV -> house / battery /
  grid wattage and the current import/export rate.

Data: one guarded `hass.callWS` history fetch per layer source, LTS
statistics for the consumption baseline, live states thereafter. Each missing
feed drops its layer with a one-line legend note; a failed fetch costs one
layer, never the card.

## Money sensors (Python)

Four per inverter, computed in the coordinator, unique_id `{serial}_{key}`
(so the strategy resolves them with its existing registry machinery):

- `grid_import_cost_today` — import energy × import rate, integrated
  incrementally per coordinator tick.
- `grid_export_earnings_today` — same against the export rate.
- `net_energy_cost_today` — import cost − export earnings. The headline.
- `counterfactual_cost_today` — today's house consumption priced straight at
  the import rate, ignoring battery and solar. Attribute `savings_today` =
  counterfactual − net.

Semantics:

- Each tick prices the energy delta of the relevant `_today` source sensor at
  the rate in force; a negative delta means the source reset at midnight ->
  reset the accumulator.
- `RestoreSensor` carries the running total across restarts; a restore from a
  previous day starts at 0.
- Rate units: £/kWh or p/kWh accepted, normalised from the rate entity's
  `unit_of_measurement`. `device_class=MONETARY`, `state_class=TOTAL` with
  midnight `last_reset` — month-to-date falls out of LTS for free.
- Tariff entity unavailable/unknown -> the money sensors go unavailable
  rather than accumulating priced-at-zero garbage; they resume from the
  running total when rates return.

Configuration: a new options flow with two optional entity pickers (import
rate, export rate). Left blank, the sensors are not created and the
integration is unaffected.

## Degradation summary

| Missing | Effect |
| --- | --- |
| Tariff entities (card config) | Band tints + rate labels drop; tape still works |
| Solar forecast | Future solar layer drops; SOC projection uses slots + baseline only |
| Tariff entities (options) | Money sensors not created (blank) or unavailable (outage) |
| Statistics for an Observatory sensor | That card omitted |
| Registry fetch failure | Existing friendly-notice view |

## Build order (independently shippable PRs)

1. Money sensors + options flow (+ this spec).
2. `ge-tape.js` (tape + mission cards) + `mode: mission` emitting Mission
   Control + Tape views.
3. Ledger card + view.
4. Observatory view (strategy-side composition only).
5. Polish: next-action heuristics, docs, screenshots.

## Testing

- **vitest**: mission mode emission (view set, no dangling entity refs,
  config passthrough, EMS fallback); tape pure helpers (window arithmetic,
  downsampling, event detection, SOC projection, rate-attribute parsing)
  against canned fixtures.
- **pytest**: money-sensor pricing across rate changes, midnight reset,
  restart restore, tariff outage, p/kWh vs £/kWh, counterfactual arithmetic;
  options flow; manifest/key-parity guards extended to any new strategy keys.

## Out of scope

No tariff/forecast autodetection; no inferred ledger line items; no
recommendations engine; no changes to existing modes; `mode: all` stays on
its deprecation path.
