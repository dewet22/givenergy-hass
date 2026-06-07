# Release notes

Human-readable notes for each release, newest first. From v1.1.5 onward the release
tag carries only a short summary and links here for the full notes and screenshots;
opening the file at a tag's pinned ref (`…/blob/vX.Y.Z/RELEASE_NOTES.md`) lands on
that release. For releases prior to v1.1.0, see the
[GitHub Releases](https://github.com/dewet22/givenergy-hass/releases) page.

---

## v1.1.5

The dashboard strategy gains two new full-screen modes — **Glance** and **Analyst** —
alongside the existing `flow` and `classic` layouts, plus a fix to the flow diagram's
centre labels.

**Glance mode**

A new `mode: glance` option for the `custom:givenergy` dashboard strategy leads with a calm, full-viewport Glance panel: a single-sentence system summary, three large numbers (solar generated today, battery state-of-charge, house consumption today), and a row of health pills covering battery count, the day's grid import and export totals, and per-string PV generation when active. It's built around a new bundled `custom:givenergy-glance` element — nothing extra to install — and every value is resolved through the entity registry, consistent with the rot-immunity approach introduced in v1.1.3. Set `strategy: { type: custom:givenergy, mode: glance }`.

![Glance mode](docs/dashboard-glance.png)

The summary sentence is derived from the live signs of grid, battery, and solar power, covering states like self-sufficient, exporting, importing on solar-and-grid, and battery-only overnight. The status dot pulses green when the system is self-sufficient or exporting, and amber when importing from the grid or when battery SOC drops below 20%. Like flow mode, the Glance view is a full panel and picks up kiosk-mode hints when that integration is present.

**Analyst mode**

A new `mode: analyst` option leads with a dense Analyst view aimed at optimisation and debugging: a live metrics strip (PV, load, battery, grid), an energy ledger breaking today's sources and sinks down as kWh and percentages, a diagnostics table (temperatures, grid frequency, power factor, work time, consecutive failures), a 24-hour power overlay chart (requires `apexcharts-card`), and per-pack cell heatmaps. It's a standard (non-panel) multi-card view, so the full classic tab set still follows it. Set `strategy: { type: custom:givenergy, mode: analyst }`.

![Analyst mode](docs/dashboard-analyst.png)

**Flow: clearer labels when streams cross the centre**

The flow diagram's two centre streams — solar→battery (vertical) and grid→home (horizontal) — share a midpoint, so when both were active their kW labels overlapped into an unreadable run-together. Each label now offsets perpendicular to its own axis, keeping both values legible when the system is charging from solar while importing from the grid.

**Deprecation: the static dashboard generator**

The `generate_dashboard` service is deprecated in favour of the live dashboard strategy and will be removed in a future release. It still works for now, but logs a warning when called and its persistent notification points to the strategy. If you're on a generated static dashboard, switch over by setting a dashboard's raw config to `strategy: { type: custom:givenergy }`.

**Maintenance**

Bundles givenergy-modbus 2.1.3 (unchanged). Also bumps the dev-only test toolchain (vitest 2 → 4, with transitive vite and esbuild) to clear three Dependabot advisories in the JS test dependencies — no runtime or packaged-integration change.

## v1.1.4

**Flow mode for the dashboard strategy**

A new `mode: flow` option for the `custom:givenergy` dashboard strategy prepends a full-viewport Energy Flow panel to the existing classic tabs. The panel is built around a new `custom:givenergy-flow` element — no additional card to install — that renders three header tiles (solar generation with per-string breakdown, combined battery state-of-charge with per-pack percentages, and home load with a live import/export direction sentence), an animated SVG flow diagram, and a today-totals energy strip. All entity slots are resolved via the registry, consistent with the rot-immunity approach introduced in v1.1.3.

To use it: create a dashboard, open the raw configuration editor, and set `strategy: { type: custom:givenergy, mode: flow }`. The classic tabs still follow the Flow panel and remain accessible.

**Animated energy paths with correct flow decomposition**

The flow diagram resolves seven directed edges using a solar-first priority: solar fills battery charging first, then covers any grid export, then feeds home directly; grid covers remaining import; battery discharge covers remaining home load or feeds back to the grid. Each path is colour-coded (amber for solar generation, green for export, red for import, blue for charging, purple for discharging) and magnitude-scaled — thicker strokes and faster animation for higher power — so the diagram conveys both direction and quantity at a glance. Inactive paths render as subtle dashed outlines rather than disappearing, keeping the full topology readable when flows are low.

**Self-hosted Fraunces and Geist Mono fonts**

The numeric values use a glyph-subsetted Fraunces woff2 (~12 KB) and the edge labels use Geist Mono (~7 KB), both served directly by the integration at `/givenergy_local/fonts/` with no third-party font requests. Both fonts are OFL-licensed; licence files are included.

**Dependency**

Bundles givenergy-modbus 2.1.3.

## v1.1.3

**Live dashboard strategy**

A new Lovelace dashboard strategy (`custom:givenergy`) builds the full dashboard from the live entity registry on every render, resolving each entity by its stable unique_id rather than a frozen entity_id. It can't go stale when a device is moved between areas or an entity is renamed — the failure mode that left the static dashboard full of "entity not available" rows once HA 2026.6 began folding a device's area into its entity_ids. Create a dashboard, open the raw configuration editor, and set `strategy: { type: custom:givenergy }`. The `generate_dashboard` service remains as an editable static starting point. One caveat: on a hard browser refresh the strategy can occasionally hit Home Assistant's 5-second "strategy element" registration timeout — a limitation common to all network-loaded strategies; a normal reload serves it from cache and isn't affected.

**A fuller generated dashboard**

The generated dashboard gained substantial coverage: Smart Load and AC-coupled controls, battery power/pause mode controls and all-time energy totals; battery out-of-spec status, AC output telemetry and battery maintenance mode; and per-string PV, three-phase and EPS diagnostics plus solar diverter energy. The bundled cell-balance heatmap card is served by the integration, so there's nothing extra to install for it.

**Dashboards survive area assignment and renames**

For installs staying on the generated YAML, the generator now resolves its entity references through the registry as well, so assigning an inverter to an area (which HA 2026.6 folds into the entity_id) no longer breaks a pasted dashboard.

**Grid Power is now a signed net value**

"Grid Export Power" has been renamed to "Grid Power" and now reports signed net flow — positive when exporting, negative when importing — matching what the underlying register actually measures. The existing entity and its history are migrated in place under the new slug, so no history is lost.

**Stable entity_ids for control entities**

Number, select, switch and time entities now carry their device name in DeviceInfo, keeping their entity_ids stable across restarts and bringing them in line with the sensor platform.

**Charge-cycle history from GivTCP (did not work; later removed)**

This release added a charge-cycle pair to the GivTCP statistics migration script, meant to carry each battery's cycle count across. It never actually worked: GivTCP records cycles as a *mean* statistic, but givenergy_local's `charge_cycles` is `total_increasing` (a sum series), so battery detection never matched the source and nothing was copied. The pair was removed in a later migration-script update — cycle history is not migrated.

**Dependency**

Bundles givenergy-modbus 2.1.3.

## v1.1.2

**EMS: export power limit control**
An Export Power Limit number entity (0–6000 W, 100 W steps) is now created on EMS plants, exposing the inverter's grid export cap as a configurable control directly from the HA dashboard.

**Battery health: out-of-spec alert sensor**
A new *Battery Out of Spec* binary sensor (`device_class: problem`) monitors cell voltages (3.0–3.5 V) and cell-group temperatures (0–50 °C) across all connected packs. It uses a hybrid debounce — the sensor only trips after a reading has been out of range for at least 5 minutes *and* across at least 3 consecutive polls, which filters out the transient bad reads that GivEnergy dongles can occasionally produce. Offending cells/groups and their duration are listed in the sensor's attributes even before the debounce fires.

**Debug capture: landing page and signed download**
`capture_frames` now produces a proper inspection page rather than a bare file in `/local/`. The persistent notification links to a signed landing page showing the environment header (HA version, Python, OS, integration and library versions), an inline frame dump, a one-click download, and a pre-filled GitHub issue link. Captures are stored in `<config>/givenergy_local_captures/` rather than the publicly-accessible `www/` directory.

**Three-phase: suppress single-phase-only sensors**
On three-phase inverters, the combined PV Power, PV Energy Today, and Battery Nominal Capacity sensors are no longer created — they were derived from single-phase assumptions and rendered as permanently-unavailable orphan entities on three-phase hardware. Per-string sensors (PV String 1/2) remain.

**givenergy-modbus 2.1.3**
This release requires 2.1.3, which brings resilience fixes: unmapped enum values no longer crash the library, and Smart Load slot polling is gated correctly.

## v1.1.1

Fixes house-consumption reporting and adopts givenergy-modbus 2.1.1.

The consumption figure read near-zero on single-phase inverters because the
underlying register (e_load_day / IR35) was a GivTCP-era mislabel — it's actually
AC charge, not house load. givenergy-modbus 2.1.1 corrected this and added the
real derived consumption (PV generation + grid import − grid export − AC charge,
matching the GivEnergy app's "Consumption today").

This release:
- Adds a House Consumption Today sensor with the correct derived value — the
  dashboard's "Consumed" series now reflects real consumption.
- Renames the old "Load Energy Today" sensor to AC Charge Today (its true
  meaning), preserving existing history via an automatic entity migration.
- Picks up the modbus 2.1.1 EMS per-slot status fix (#108).

No action needed on update — entities migrate automatically.

## v1.1.0

This is a substantial release — the integration moves onto the `givenergy-modbus`
2.1 line, and with it comes first-class support for EMS plants, AC-coupled
inverters, and All-in-One units, alongside a much more resilient polling and
detection path. If you're upgrading from 1.0.x, everything below is new.

### EMS plant support

The biggest addition. EMS (Flexi / Plant) installations now get proper
representation:

- **Plant-level scheduling** — charge, discharge, and export slots (1–3) are
  exposed as configurable time entities, each with its own SoC-target control
  (#76, #83).
- **The EMS controller gets its own device identity** and a tailored dashboard,
  rather than being folded into the inverter (#96).
- **Flexi / export parity knobs** — RTC, active-power-rate, export and flexi
  controls that mirror what the GivEnergy portal exposes (#83).

### AC-coupled and All-in-One inverter controls

- **Export priority and EPS controls** are now available on AC-coupled inverters
  (#90).
- **AC-coupled battery charge/discharge limits** are exposed, gated to the
  inverter types that actually have the AC-config register block (#89). All-in-One
  units are included in this gating after #99 — they share the same config block,
  so they now get the same controls.

### Smart Load scheduling

- **Smart Load slots 1–10** are exposed as configurable start/end time entities,
  mirroring the charge/discharge slot controls (#106). These appear on non-EMS
  plants; on an EMS plant the controller owns scheduling, so its slot entities are
  used instead.

### New services

- **`set_system_datetime`** — sync the inverter's clock to Home Assistant's (#87).
- **`expose_recommended_entities`** — opt-in helper to surface a curated,
  voice/LLM-friendly entity set, with accompanying docs (#65, #66).

### Reliability and detection

This release reworks how the integration handles imperfect polls and topology
detection — the practical upshot is far fewer spurious failures and disappearing
devices:

- **Partial polls no longer brick the integration.** A poll that partially
  succeeds now loads the usable data and marks only the failed reads unavailable,
  instead of failing the whole setup (#71, #97). The investigation surface is the
  unavailable entity, not a looping integration.
- **A slow-responding BMS no longer drops a battery.** Detection continues past a
  pack that's slow or briefly absent rather than stopping at the first miss — the
  root-cause fix for a second battery vanishing after a reconnect (#100, with the
  underlying library fix).
- **Plant topology persists across restarts** (#48, #62) — warm starts skip the
  full detection sweep, and a transient under-count no longer permanently reduces
  your topology.
- The partial-refresh warning is **throttled** so a flaky connection doesn't flood
  the log (#86).
- Battery-energy sensors are reconciled with the renamed library fields (#93), and
  the inverter status sensor is guarded against `None` (#73).

### Sensors and dashboards

- **Sensors now render at their native register precision** rather than a fixed
  rounding (#72).
- **A cross-pack Battery Health dashboard view** ships with a bundled cell-voltage
  heatmap card, registered at component scope so it survives reloads (#79, #91).

### Documentation

- A **GivTCP → `givenergy_local` migration catalogue and script** (issue #67),
  plus reframed passive-mode and parallel-running notes.
- **HACS pre-release install steps** added to the README (#77).

### Platform and dependencies

- **`givenergy-modbus` moves from 2.0.6 to 2.1.0** — the enabling change behind
  most of the above.
- **Minimum Python is now 3.14.2** (was 3.14).

### Upgrade notes

- No manual migration is required — entities and devices are created automatically
  on the first poll after the update.
- The HR(199) field was corrected library-side from
  `enable_standard_self_consumption_logic` to `enable_inverter_parallel_mode`; no
  entity currently reads it, so there's no user-facing change. The entity-ID rename
  it implies is deferred to a future per-type device-naming migration to avoid
  putting anyone through it twice.
