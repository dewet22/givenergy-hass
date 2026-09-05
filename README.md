# GivEnergy Home Assistant Integration

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/logo.png" alt="GivEnergy" width="320"></p>

[![release](https://img.shields.io/github/v/release/dewet22/givenergy-hass)](https://github.com/dewet22/givenergy-hass/releases)
[![CI](https://github.com/dewet22/givenergy-hass/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/dewet22/givenergy-hass/actions?query=branch%3Amain)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/dewet22/givenergy-hass/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/use/repositories/dashboard)

Talk to your GivEnergy inverter straight over local Modbus TCP — **no cloud, no GivEnergy portal account, no MQTT broker.** Full sensor and control coverage, a live self-building dashboard, and proper Home Assistant plumbing for automations, the Energy dashboard, voice and LLMs.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/dashboard-flow.png" alt="GivEnergy dashboard — flow mode" width="820"></p>

## ✨ Highlights

- 🔒 **Fully local & private** — talks to the inverter directly on your LAN; nothing leaves the house, and no GivEnergy account is involved.
- 🔋 **Every sensor and control** — PV, battery, grid and load telemetry; per-cell battery health; charge/discharge scheduling, SOC targets, and battery modes.
- 📊 **A dashboard that builds itself** — a bundled strategy renders a full six-tab layout (plus Flow, Glance and Analyst views) and resolves every entity live, so it never rots when a device moves or an entity gets renamed.
- 🤖 **Predbat- & EMS-ready** — drives Predbat on single- and multi-inverter EMS plants, with no GivTCP or MQTT broker in the loop.
- 🗣️ **Voice & LLM in one click** — a single service exposes the right entities to Assist and LLM tools (Claude / OpenAI via MCP).
- 🔬 **Honest about the hardware** — register maps are confirmed against real wire captures, with built-in capture tooling to diagnose anything that reads wrong.

Works with single-phase and three-phase hybrids, AC-coupled, All-in-One, EMS and Gateway systems — see [Supported inverters](#supported-inverters) for what's confirmed on real hardware.

## Part of a local-first stack

This integration sits in a cloud-free GivEnergy ecosystem, with a hardware-agnostic energy layer growing on top of it:

- **[givenergy-modbus](https://github.com/dewet22/givenergy-modbus)** — the Python library underneath it all: local Modbus TCP, register decoding, and the hardware-capability model this integration is built on.
- **[givenergy-cli](https://github.com/dewet22/givenergy-cli)** — a terminal companion for the same library: probe registers, capture wire frames, export a full plant dump, or drive the inverter without Home Assistant.
- **[Energy Conductor](https://github.com/dewet22/ha-energy-conductor)** — entity-driven, technology-agnostic energy coordination for Home Assistant. It only ever talks to the entities your integrations expose — GivEnergy or anything else — layering a polished UI, forecasting and automated control over whatever hardware you already run. Early days and actively developed; worth a look if you'd like to help shape where it goes.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/energy-conductor-forecast.png" alt="Energy Conductor — the tape: 12h history, 12h forecast" width="840"></p>
<p align="center"><em>The <b>tape</b> — twelve hours of history and twelve of forecast on one axis, with the events it's scheduling (off-peak, EV, solar diversion) laid out beneath.</em></p>

<details>
<summary><b>More of Energy Conductor</b></summary>

**Tonight's plan in plain English, plus live control** — it doesn't just visualise, it decides, actuates, and tells you why.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/energy-conductor-plan.png" alt="Energy Conductor — plan reasoning and control status" width="740"></p>

**The money** — billing-grade costs alongside modelled savings, down to a "paying for itself" run-rate.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/energy-conductor-ledger.png" alt="Energy Conductor — cost and savings ledger" width="440"></p>

**A year at a glance** — calendar heatmaps and per-hour density for every stream (PV, load, grid, EV, hot water, gas): a whole year of energy in one straightforward view.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/energy-conductor-history.png" alt="Energy Conductor — long-term energy analytics" width="740"></p>

</details>

## Requirements

- A [supported GivEnergy inverter](#supported-inverters) on your local network (wifi or ethernet), with its Modbus TCP port reachable from Home Assistant (default **8899**)
- Home Assistant 2026.5 or later (it ships the Python 3.14 this integration needs)

Inverter communication is handled by the [`givenergy-modbus`](https://github.com/dewet22/givenergy-modbus) library.

## Supported inverters

`givenergy-modbus` models these device families: single-phase hybrid, three-phase hybrid, AC-coupled, EMS, Gateway, and All-in-One. The register maps were originally brought in from the GivTCP fork, and a growing share has since been confirmed against wire captures from real hardware — much of it thanks to owners contributing captures and testing pre-releases ([#52](https://github.com/dewet22/givenergy-hass/issues/52), [#95](https://github.com/dewet22/givenergy-hass/issues/95)).

Confirmed working on real hardware:

- Hybrid single-phase (Gen 1)
- AC-coupled (Gen 1)
- All-in-One (AIO) — including per-module battery devices with per-cell data; needs v1.2.0 or later
- EMS controller — validated on a live two-inverter EMS plant; some polling rough edges on busy shared buses are still being smoothed out in the library
- Hybrid three-phase — per-phase electricals, HV stacks with per-cell BMU data
- HV battery stacks (BCU/BMU) — decoded per module, at their own device addresses
- Gateway (Gen 1) — validated on a Gen 1 Gateway fronting two AIOs; exposes whole-house load/PV power and energy, grid import/export, lifetime totals, and per-AIO SOC and charge/discharge (needs v1.3.39 or later). No controls are offered through a Gateway yet

The following have modelled register maps and are expected to work, but haven't yet been validated end-to-end by an owner — if yours is one of these and you run into sensor values that look wrong, please [open an issue](https://github.com/dewet22/givenergy-hass/issues):

- Hybrid single-phase Gen 2 — modelled, but no Gen 2 unit has ever been observed; a field report from an EA-prefix serial would settle its detection code
- Polar, AIO Commercial, EMS Commercial, and 3-phase AC — model codes known, decode unverified against real hardware

### Help validate your hardware

Real-world testing on non-Hybrid-Gen-1 hardware (AC, AC3, EMS, Gateway, All-in-One) is especially appreciated, and a wire-frame capture is the single most useful thing you can attach. If the integration is already running, use the built-in action from **Developer Tools → Actions** (named **Services** in older Home Assistant versions):

```
Service: givenergy_local.capture_frames
Duration: 60
```

This records a redacted copy of the raw Modbus traffic (serial numbers zeroed), saves it to `<config>/givenergy_local_captures/`, and sends a persistent notification linking to a landing page where you can inspect the capture, download the file, or open a pre-filled GitHub issue. Attach the file to the issue along with your inverter model and serial prefix.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/capture-notification.png" alt="Persistent notification when a capture completes" width="500"></p>

Following the link opens the capture's landing page:

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/capture-landing.png" alt="Capture landing page — inspect, download, or file an issue" width="560"></p>

If you don't yet have the integration installed, [givenergy-cli](https://github.com/dewet22/givenergy-cli) can produce a structured register dump instead:

```bash
uvx givenergy-cli --host <inverter-ip> export -o plant.json
```

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dewet22&repository=givenergy-hass&category=integration)

Or if that doesn't work:

1. In HACS, go to **Integrations → Custom repositories**
2. Add `https://github.com/dewet22/givenergy-hass` and select category **Integration**
3. Install **GivEnergy Local** and restart Home Assistant

### Installing a beta / pre-release via HACS

New features often ship as a pre-release (e.g. `v1.4.0rc2`) before a stable release. To install one:

1. Open **GivEnergy Local** in HACS, then the **⋮ menu (top-right) → Redownload**
2. Expand **"Need a different version?"**
3. Pick the pre-release from the **Release** dropdown (they're tagged with a **pre-release** badge)
4. **Download**, then **restart Home Assistant** (custom-component changes only apply after a restart)

### Manual

1. Download [`givenergy_local.zip`](https://github.com/dewet22/givenergy-hass/releases/latest/download/givenergy_local.zip) from the latest release
2. Extract its contents into your Home Assistant `config/custom_components/givenergy_local/` folder
3. Restart Home Assistant

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → GivEnergy Local**.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/config-flow.png" alt="Config flow — add integration dialog" width="440"></p>

| Field | Default | Description |
|---|---|---|
| Inverter IP Address | — | Local IP of the inverter's data adapter |
| Modbus Port | `8899` | Modbus TCP port |
| Scan Interval | `30` s | How often HA polls for updated values |

To change any of these later, open the integration's **⋮** menu in **Settings → Devices & Services → GivEnergy Local** and choose **Reconfigure**. The integration reloads automatically when you save.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/config-reconfigure.png" alt="Integration ⋮ menu — Reconfigure entry" width="340"></p>

### Running alongside other local polling clients

Both this integration and other local polling solutions (GivTCP, the GivEnergy app, custom scripts) can run in active mode at the same time without issue. The inverter handles concurrent Modbus clients reliably — earlier reports of polling conflicts were largely a consequence of retry and error-recovery behaviour in the older shared library, not an inverter limitation. Running multiple clients in parallel has been solid in practice, even at faster-than-default poll intervals.

> **Note:** an optional **passive (listen-only) mode** is available under *Advanced features*. Rather than polling, it reads the register cache that another active client (for example GivTCP) keeps fresh on the same dongle — halving the polling load, which is worth having on a marginal dongle that struggles under two active clients. It was briefly removed in v1.3.37 because, with only the GivEnergy cloud's ~5-minute polling to listen to, its freshness expectations couldn't be met; it's back because that reasoning doesn't apply when a local client is actively polling. It needs another client actively polling the same inverter — on its own, values will go stale. The same section holds the **reconnect quiet window** (default and minimum 120 seconds): after two consecutive failed liveness probes the integration stops reconnecting for that long so a marginal dongle can recover undisturbed. Raise it if a dongle keeps locking up; the `Dongle Hangs` and `Reconnect Backoffs` counters show whether it helps.

#### GivTCP long-term stats migration

Running both integrations in parallel makes it easy to evaluate a switch without committing: you get a live side-by-side comparison and can cut over at your own pace. A migration script (`scripts/migrate_from_givtcp.py`) carries your long-term recorder statistics across — see [`docs/migration-from-givtcp.md`](docs/migration-from-givtcp.md) for usage.

## Dashboard

The recommended way to set up a full dashboard is the live **dashboard strategy**. It builds the complete six-tab layout but resolves every entity from the registry each time the dashboard loads, so it never goes stale when a device moves between areas or an entity is renamed — the failure mode that left static dashboards full of "entity not available" rows once Home Assistant 2026.6 began folding a device's area into its entity IDs. (I added it because the static YAML kept silently rotting on my own install after area reassignments.)

To use it, create a new dashboard, open the **raw configuration editor**, and set the whole config to:

```yaml
strategy:
  type: custom:givenergy
  mode: classic        # classic (default) | flow | glance | analyst | all — see below
  max_power_kw: 10     # optional; default 10; Overview 24h chart y-axis envelope (kW)
  serial: SA1234G000   # optional; pin one inverter on a multi-plant install
```

The strategy and the bundled cell-heatmap card are served by the integration itself, so there's nothing extra to install for them. `power-flow-card-plus` and `apexcharts-card` are still needed for the Overview/Energy charts (install them via **HACS → Frontend**); where they're missing the strategy shows a short placeholder rather than a broken card.

One caveat worth knowing: on any **cold (uncached) load** the dashboard may show "Error loading the dashboard strategy: Timeout waiting for strategy element …". This is a Home Assistant limitation common to all network-loaded dashboard strategies — HA gives the strategy module a fixed 5-second window to register, and an uncached fetch frequently loses that race when it's queued behind other frontend resources. A cold load happens on a browser hard refresh (Ctrl/Cmd+Shift+R), but also on any client that has simply never loaded the module before — a fresh install, a new device, or the companion app. The fix is inelegant but genuine: **retry until it loads once** — reload in a browser; in the app, navigate away and back, pull to refresh, or close and reopen. You're not trying to bypass the cache, you're trying to fill it: the first successful load caches the module, after which it registers instantly and the dashboard stays reliable.

### `mode: flow`

`mode: flow` leads the dashboard with an immersive, full-width **Flow** view — an animated power-flow diagram (solar, grid, battery, home) with the live direction of each flow derived from the sign of the underlying power sensors, three big-number headers, and a today-totals strip. It's a bundled custom card (`custom:givenergy-flow`), so nothing extra to install. The full `classic` view set (Overview, Energy, Batteries, Battery Health, Controls, Diagnostics) follows behind it, so you lose nothing by switching.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/dashboard-flow.png" alt="GivEnergy dashboard — flow mode" width="820"></p>

```yaml
strategy:
  type: custom:givenergy
  mode: flow
```

The Flow view is rendered as a `panel: true` view. If you have the **kiosk-mode** custom integration installed (HACS), the strategy adds hints to hide the header and sidebar for a true full-screen display; without it, the view simply renders inside the normal HA chrome. The card is responsive (container-query based), so it works as a wall-tablet kiosk and reflows for a phone webview.

The tariff-aware `coach` direction from [the redesign brief](docs/design/dashboard-redesign-brief.md) is still to come.

### `mode: glance`

`mode: glance` leads the dashboard with a calm, full-width **Glance** view: a single-sentence system summary, three large numbers (solar generated today, battery SOC, house consumption today), and a row of health pills showing battery count, import and export totals for the day, and per-string PV generation when active. It's built around a bundled `custom:givenergy-glance` card — nothing extra to install.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/dashboard-glance.png" alt="GivEnergy dashboard — glance mode" width="820"></p>

```yaml
strategy:
  type: custom:givenergy
  mode: glance
```

The status sentence is derived from the live signs of grid, battery, and solar power — covering states like self-sufficient, exporting, solar-and-grid importing, battery-only overnight, and so on. The dot to its left pulses green when the system is self-sufficient or exporting, amber when importing from the grid or when battery SOC drops below 20%. The full classic view set follows the Glance panel, so the detailed tabs are still one tap away. Like `flow`, the Glance view is `panel: true` and picks up kiosk-mode hints when the integration is present.

### `mode: analyst`

`mode: analyst` leads the dashboard with a dense **Analyst** view aimed at optimisation and debugging: a live metrics strip (PV, load, battery, grid), an energy ledger breaking down today's sources and sinks as kWh and percentages, a diagnostics table (temperatures, grid frequency, power factor, work time, consecutive failures), a 24-hour power overlay chart (requires `apexcharts-card`), and per-pack cell heatmaps. Nothing extra to install beyond the apexcharts card for the chart.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/dashboard-analyst.png" alt="GivEnergy dashboard — analyst mode" width="820"></p>

```yaml
strategy:
  type: custom:givenergy
  mode: analyst
```

The Analyst view is a standard (non-panel) multi-card view, so the full classic tab set still follows it.

### `mode: all`

`mode: all` stacks all four views — Glance, Flow, Analyst, and the classic tab set — into a single dashboard. Useful if you want to switch between display styles without maintaining separate dashboards.

```yaml
strategy:
  type: custom:givenergy
  mode: all
```

## Entities

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/device-page.png" alt="Inverter device page in Home Assistant" width="620"></p>

### Inverter device

#### Sensors

Grouped by what they tell you. Every cumulative (kWh) sensor feeds the Energy dashboard and long-term statistics automatically.

**Solar**

| Entity | Unit | Notes |
|---|---|---|
| PV Power | W | Combined PV output |
| PV String 1 / 2 Power / Voltage / Current | W / V / A | Per-string |
| PV Energy Today / Total | kWh | |

**Battery**

| Entity | Unit | Notes |
|---|---|---|
| Battery SOC | % | |
| Battery SOC kWh | kWh | SOC × nominal capacity ([#248](https://github.com/dewet22/givenergy-hass/issues/248)) — nameplate estimate, reads optimistic on aged packs |
| Battery Power | W | Positive = discharging, negative = charging |
| Battery Voltage / Current / Temperature | V / A / °C | |
| Battery Charge / Discharge Today | kWh | |
| Battery Throughput Total | kWh | |

**Grid**

| Entity | Unit | Notes |
|---|---|---|
| Grid Power | W | Signed net flow (positive = export). Hidden by default (feeds the flow card); use the split sensors for the Energy dashboard |
| Grid Power Import / Export | W | Always-positive single-direction power for the Energy dashboard's "Two sensors" option — no sign inversion, so long-term stats aren't lost |
| Grid Import / Export Today / Total | kWh | |

**Home & inverter output**

| Entity | Unit | Notes |
|---|---|---|
| Load Power | W | |
| House Consumption Today | kWh | Derived: PV + grid import − grid export − AC charge |
| AC Charge Today | kWh | Grid energy used to charge the battery |
| Inverter Output Today / Total | kWh | |
| AC Voltage / Frequency | V / Hz | |
| Inverter Heatsink / Charger Temperature | °C | |

**Status**

| Entity | Unit | Notes |
|---|---|---|
| Status | — | Normal / Warning / Fault |
| Fault Code | — | |

Some energy sensors are model-dependent: on AC-coupled and All-in-One systems the registers historically labelled *PV Generation Today/Total* actually count the unit's battery-discharge AC output, so from v1.4.0 they appear there as **Inverter Output Today/Total** instead (existing entities are renamed in place, keeping their history), and the PV-derived sensors (Self Consumption, PV Direct) don't exist on those models.

<details>
<summary><b>Diagnostic sensors</b> — created but hidden from the default device view (~30 entities: firmware, extra electricals, error codes, refresh counters, …)</summary>

| Entity | Unit | Notes |
|---|---|---|
| Inverter Errors | — | Error bitmask |
| Charger Warning Code | — | |
| Charge Status | — | Raw int (BMS state code, mapping TBD) |
| System Mode | — | Raw int (operating mode, mapping TBD) |
| AC Output Voltage / Frequency / Current | V / Hz / A | Inverter output (post-conversion) |
| Grid Apparent Power | VA | |
| Inverter Power Factor | — | |
| Grid Power Phase 1 | W | Useful for 3-phase models |
| Export Power Rate Limit | % | Three-phase only — installer-set grid export power-rate cap (% of rated power) |
| Inverter Export Total | kWh | Cumulative inverter export to grid |
| Charge from Grid Total | kWh | Cumulative grid-sourced battery charging |
| Backup Power | W | EPS port output |
| Combined Generation Power | W | Solar + battery combined |
| Work Time Total | h | |
| Device Type Code | — | |
| MPPT Count | — | |
| Phase Count | — | 1 for single-phase, 3 for three-phase |
| ARM / DSP / Modbus Firmware Version | — | |
| Meter Type | — | CT-or-EM418 / EM115 |
| Battery Type | — | Lithium / Lead-Acid |
| Battery Capacity | Ah | Reported pack capacity |
| Battery Nominal Capacity | kWh | Ah × nominal voltage |
| Last Successful Refresh | timestamp | |
| Consecutive Refresh Failures | — | Resets to 0 on the next good poll |
| Total Refresh Failures | — | Ever-increasing (resets only on HA restart — long-term stats handle that) |
| Reconnects | — | Times the connection was re-established after a drop |
| Dongle Hangs | — | Reconnects where the dongle answered TCP but not its identity read (a Modbus-level hang) |
| Reconnect Backoffs | — | Sustained hangs where reconnecting was paused to let the dongle recover |

</details>

#### Controls

| Entity | Type | Notes |
|---|---|---|
| Enable Charge | Switch | |
| Enable Discharge | Switch | |
| Charge Target SOC | Number | 4–100 % |
| Battery SOC Reserve | Number | 4–100 % |
| Battery Charge Rate Limit | Number | Battery C-rate (C/100; 50 = 0.5C) |
| Battery Discharge Rate Limit | Number | Battery C-rate (C/100; 50 = 0.5C) |
| Battery Discharge Min Power Reserve | Number | 4–100 % |
| Battery Power Mode | Select | Export / Self Consumption |
| Battery Pause Mode | Select | Disabled / Pause Charge / Pause Discharge / Pause Both |
| Charge Slot 1 & 2 Start / End | Time | |
| Discharge Slot 1 & 2 Start / End | Time | |
| Battery Pause Slot Start / End | Time | Active window for the pause mode above |

### Battery device(s)

Each connected battery pack appears as a separate device linked to the inverter. On All-in-One (AIO) hardware, each removable battery module is also surfaced as its own device (linked to the AIO inverter), exposing its `HX…` serial and per-cell voltages and temperatures — see [AIO battery modules](#aio-battery-modules) below ([#192](https://github.com/dewet22/givenergy-hass/issues/192)).

| Entity | Unit | Notes |
|---|---|---|
| SOC | % | |
| Voltage | V | Pack output voltage |
| Temperature Max / Min | °C | |
| Remaining Capacity | Ah | |
| Design Capacity | Ah | |
| Charge Cycles | — | |

<details>
<summary><b>Cell-level diagnostics</b> — per-cell voltages and temperatures, hidden from the default view but ideal for pack-health monitoring (cell voltage spread, temperature deltas)</summary>

| Entity | Unit | Notes |
|---|---|---|
| Cell Count | — | Number of cells the BMS reports |
| Cell Voltages Sum | V | Sanity-check against Voltage |
| BMS MOSFET Temperature | °C | |
| Cell 1 … 16 Voltage | V | Per-cell; unused positions in smaller packs read ~0 |
| Cells 1-4 / 5-8 / 9-12 / 13-16 Temperature | °C | The BMS samples one thermistor per 4-cell group |

</details>

#### AIO battery modules

All-in-One systems expose each removable battery module separately, so on AIO hardware you'll see one extra device per module (parented to the AIO inverter) alongside the pack-level battery device. Each module device carries its `HX…` serial plus 24 per-cell voltages and 12 per-cell temperatures, all diagnostic. These mirror the LV per-cell entities, so the cell-heatmap card and the same pack-health views work at module granularity. Module data needs v1.2.0 or later.

### EMS controller device

On an EMS plant the controller device carries plant-level aggregates the inverter registers don't: managed-inverter count, calculated/measured load power, grid meter power, total battery power and remaining battery energy. Since the EMS rollup carries no energy counters, several energy sensors are derived by integrating those live power signals client-side:

| Entity | Unit | Notes |
|---|---|---|
| EMS Calculated Load Energy Today / Total | kWh | Integrated client-side from *EMS Calculated Load Power* — the EMS rollup carries no energy counters, so no register-backed figure exists. An estimate, not a meter reading: it undercounts across restarts and outages (a sampling gap contributes at most one capped slice), though the accumulated value does survive Home Assistant restarts. |
| EMS Battery Charge / Discharge Today / Total | kWh | Integrated client-side from *EMS Total Battery Power*, split by sign into charging and discharging energy — the pair the Home Assistant Energy dashboard's battery section expects. Same estimate caveats as the load energy above: undercounts across gaps, survives restarts. |

These replace the Integral + Utility Meter helpers a Predbat EMS setup previously had to hand-create ([#248](https://github.com/dewet22/givenergy-hass/issues/248), [#275](https://github.com/dewet22/givenergy-hass/issues/275)).

Predbat expects its inverter mapping in `apps.yaml`, and it ships templates for GivTCP and the GivEnergy cloud, but not yet for this integration. Rather than hand-author it (and chase entity-ID drift when things get renamed), the integration can **generate a starting `apps.yaml` from your live install**: run **Developer Tools → Actions → "GivEnergy Local: Generate Predbat Config"**, or tap **Generate Predbat Config** on the dashboard's Diagnostics tab. It resolves the correct entity IDs, serials and area for your topology (single-inverter hybrid, AC-coupled, or EMS) and sends a persistent notification linking to a page where you can review and download the file. You still complete the tariff, rates, solcast and other Predbat settings — see the comments in the generated file and the [Predbat docs](https://springfall2008.github.io/batpred/). It follows Predbat's documented field spec and the register semantics worked out with testers, but I don't run Predbat myself — so it hasn't been validated end-to-end, and is best treated as a well-formed starting point rather than a guaranteed-working config. Reports on how it fares (especially whether Predbat can actually control the inverter through it) are very welcome so it can be firmed up.

On an EMS + Predbat setup, one portal-side step matters: set each inverter's own AC-charge slot 1 and DC-discharge slot 1 to 00:00–23:59 in the GivEnergy portal, or the inverters ignore EMS charge commands that fall outside their own slot windows (the generated file repeats this note). Multi-inverter (non-EMS), three-phase and Gateway plants aren't templated yet — the generator emits a short pointer for those. With thanks to the community testers on [#95](https://github.com/dewet22/givenergy-hass/issues/95) / [#281](https://github.com/dewet22/givenergy-hass/issues/281) / [#289](https://github.com/dewet22/givenergy-hass/issues/289) whose configs and register work settled the mapping.

### Gateway device

An entry pointed at a Gen 1 Gateway surfaces whole-house telemetry from the Gateway's own registers ([#194](https://github.com/dewet22/givenergy-hass/issues/194)): load and PV power, today's and lifetime load / PV / grid import / grid export / battery charge / battery discharge energy, grid and load voltage, and — per attached AIO — battery SOC, power, and today's/lifetime charge/discharge energy (unpopulated AIO slots are omitted). Two energy sets coexist by design: *Battery Charge/Discharge* figures are battery-DC energy, while the *AC Charge/Discharge* figures are the AC-side site set — conversion losses sit between them, and on sites with a GivEnergy EV charger the AC-side charge figure includes EV charging energy. The standard inverter sensor set is suppressed on a Gateway entry: the Gateway's inverter-shaped registers read as zeros, and the real data lives in the Gateway banks. No control entities are offered through a Gateway yet. Your per-AIO entries (typically in battery-data-only mode) continue to work alongside.

### Services

The integration registers the following services under the `givenergy_local` domain. All are accessible from **Developer Tools → Actions** (named **Services** in older Home Assistant versions) or from automations.

| Service | Description |
|---|---|
| `givenergy_local.set_system_datetime` | Syncs the inverter's RTC clock to Home Assistant's current time. Requires a `device_id`. |
| `givenergy_local.expose_recommended_entities` | Exposes an opinionated headline set of entities (battery SOC, PV/grid/load power, today's and lifetime energy totals, inverter status) to one or more voice/LLM assistants. Defaults to the `conversation` assistant (Assist, the LLM tools API, MCP-via-conversation); pass `assistants` to override. See **Voice assistants & LLM access** below. |
| `givenergy_local.redetect_plant` | Clears the cached plant topology for the inverter and reloads the integration, forcing a full hardware-detection sweep. Use after adding or removing a battery. Requires a `device_id`. |
| `givenergy_local.capture_frames` | Captures raw Modbus wire frames for a configurable duration (10–300 s, default 60 s), writes a redacted copy to `<config>/givenergy_local_captures/`, and sends a persistent notification with a download link. Serial numbers are zeroed before the file is written. Attach the file to a GitHub issue when reporting connectivity problems. |
| `givenergy_local.generate_predbat_config` | Builds a starting Predbat `apps.yaml` for this install, resolving entity IDs live from the registry (so it survives renames), writes it to `<config>/givenergy_local_predbat/`, and sends a persistent notification with a review/download link. Covers single-inverter hybrid, AC-coupled, and EMS plants. |
| `givenergy_local.reboot_inverter` | Sends the inverter reboot command. Requires a `device_id`. |
| `givenergy_local.calibrate_battery_soc` | Triggers a BMS SOC calibration cycle. Requires a `device_id`. |

### Not exposed by default

The upstream library makes ~180 inverter fields available; this integration intentionally exposes the subset that's useful for end users without being unsafe or noisy.

<details>
<summary>What's deliberately skipped for now</summary>

- `enable_*` flags for low-level inverter behaviour (buzzer, RTC, BMS read, frequency derating, auto-judge battery type, …) — changing these from a UI toggle is rarely what you actually want
- Battery calibration registers, voltage-adjust trims, low-voltage force-charge timers
- Charge / discharge slots 3 - 10 and their per-slot SOC stops (slots 1 and 2 cover typical Eco/Timed usage)
- Admin / destructive actions: inverter reboot, BMS flash update, auto-test triggers, ARM-chip select, user-code register
- Raw debug fields (internal bus voltages, countdown timers, `debug_inverter`)
- Per-phase three-phase data beyond `Grid Power Phase 1` and the three-phase balance registers

</details>

If any of these would genuinely help your setup, [open an issue](https://github.com/dewet22/givenergy-hass/issues) describing the use case — the field probably can be exposed with a single description entry, but it's nicer to have a concrete reason to do it. The same applies if a sensor we *do* expose looks wrong on your inverter — see [Help validate your hardware](#help-validate-your-hardware) for how to produce a frame capture.

## Voice assistants & LLM access

Home Assistant's voice assistants (Assist) and LLM tools (Claude / OpenAI via MCP) can only see entities that are explicitly **exposed**. HA auto-exposes a curated allowlist of sensor device classes — `temperature`, `humidity`, and a few others — but `power`, `energy`, and `battery` are **not** on that list, so none of this integration's headline sensors are visible to voice or LLM queries by default. Asking "what's my battery at?" silently returns nothing until you fix it.

### Option 1: run the `expose_recommended_entities` service (recommended)

From **Developer Tools → Actions**, pick **GivEnergy Local: Expose recommended entities to voice assistants**, choose your inverter device, and run it. You'll get a persistent notification listing what was exposed. The service is idempotent — re-run any time without losing manual customisations (it only ever exposes; it never un-exposes).

By default it targets the `conversation` assistant, which covers Assist, the LLM tools API, and MCP-via-conversation. Pass `assistants` to also target `cloud.alexa` or `cloud.google_assistant`.

The opinionated set covers ~17 entities, scoped to the questions a voice user actually asks:

| Question | Entities |
|---|---|
| "What's my battery at?" / "Is it charging?" | Battery SOC, Battery Power |
| "How much did I charge/discharge today?" | Battery Charge Today, Battery Discharge Today, Battery Throughput |
| "Am I generating?" / "Solar today?" / "Lifetime PV?" | PV Power, PV Energy Today, PV Energy Total |
| "Importing or exporting?" / "Today and lifetime grid?" | Grid Power, Grid Import Today, Grid Export Today, Grid Import Total, Grid Export Total |
| "What's the house using?" | Load Power, Load Today |
| "Lifetime inverter output?" | Inverter Output Total |
| "Is everything ok?" | Inverter Status |

Topology variation is handled implicitly: PV-only installs simply skip the battery entries, and three-phase inverters use the same keys.

### Option 2: expose manually

Go to **Settings → Voice assistants → Expose** and tick the entities you want. The list above is a good starting set.

<details>
<summary><b>Suggested aliases, and why exposure is manual</b></summary>

Default entity names like `GivEnergy Inverter SA1234G123 Battery SOC` are unwieldy for voice. After exposing, open each entity's voice-assistants tab (**Settings → Devices & Services → GivEnergy Local → \<entity\> → Aliases**) and add short aliases. A conservative starting set:

| Entity | Suggested aliases |
|---|---|
| Battery SOC | `battery`, `battery level` |
| Battery Power | `battery power` |
| PV Power | `solar`, `panels`, `pv` |
| PV Energy Today | `solar today`, `pv today` |
| Grid Power | `grid` |
| Grid Import Today | `import today` |
| Grid Export Today | `export today` |
| Load Power | `load`, `house power` |

Aliases are deliberately not shipped by the integration: the entity-registry alias field has no provenance marker, so we can't distinguish "we set this" from "the user set this" — which means we couldn't preserve user edits cleanly across restarts. Add only the aliases that match your household's vocabulary; less is usually more, since each alias becomes its own intent-match candidate.

**Why aren't these auto-exposed?** HA's conversation agent filters sensor entities by `device_class` against an allowlist tied to its intent matchers; `power`, `energy`, and `battery` aren't on the list. There's no `_attr_*` an integration can set to override this — exposure is intentionally a user-controlled decision. Background: [community thread on Assist auto-exposure](https://community.home-assistant.io/t/wth-are-all-new-entities-exposed-to-assist-by-default/803889).

</details>

## Energy dashboard

All cumulative-energy entities (kWh) are exposed with `device_class=energy` and `state_class=total_increasing`, so Home Assistant generates long-term statistics for them automatically and they show up directly in the Energy dashboard's entity picker.

<details>
<summary><b>Which entity goes in each Energy-dashboard slot</b></summary>

**Required — energy sensors (kWh, for the dashboard graphs)**

| Dashboard slot | Entity |
|---|---|
| Solar production | `PV Energy Today` (or per-string `PV String 1/2 Energy Today` if you'd rather track MPPTs individually) |
| Grid consumption | `Grid Import Today` |
| Return to grid | `Grid Export Today` |
| Home battery — energy going IN | `Battery Charge Today` |
| Home battery — energy coming OUT | `Battery Discharge Today` |

The dashboard derives household consumption automatically from the above. If you'd like to track it directly as a sanity check, `House Consumption Today` measures the derived household demand and can be added under "Individual devices".

**Optional — power sensors (W, for the "Now" live view)**

The dashboard's live view shows current power flow between solar, grid, battery and load. Wire these in once the energy mappings above are in place:

| Dashboard slot | Entity | Sign convention |
|---|---|---|
| Solar power | `PV Power` | Positive when producing |
| Grid power | `Grid Power` | Positive = exporting, negative = importing |
| Battery power | `Battery Power` | Positive = discharging, negative = charging |
| Household demand | `Load Power` | This would be universally positive, unless you have another generation source |

The daily counters reset at midnight; Home Assistant's recorder detects the reset automatically thanks to the `total_increasing` state class, so deltas across day boundaries are accounted for correctly.

</details>

## Troubleshooting

- **Comms health at a glance.** Each device exposes diagnostic counters that surface connection problems without trawling logs: `Consecutive Refresh Failures` (resets to 0 on the next good poll), `Total Refresh Failures`, `Last Successful Refresh`, per-cause counters for CRC failures, frame-splice rejections and holds, read retries and cold-start holds, plus connection-layer counters — `Reconnects`, `Dongle Hangs` (the dongle up on TCP but not answering), and `Reconnect Backoffs`. A steady trickle is normal; a sustained climb is worth a look.
- **CRC errors are normal — and are how the library knows the bus data is trustworthy.** They're almost always electrical (cable routing near current-carrying conductors, termination, shielding), *not* a symptom of too many clients: the data adapter mediates the downstream Modbus bus no matter how many clients connect. A frame that fails its CRC is simply discarded and re-read on the next tick. The odd one logged at WARNING is the guard doing its job.
- **Transient connection drops are normal.** TCP-level timeouts and the occasional reset are logged at WARNING and the next scan tick reconnects — the counters above will tell you if something more persistent is going on.
- **Control values drift back on EMS plants.** With EMS plant mode active, the controller periodically reasserts certain inverter settings (notably *Inverter Max Output Active Power*, HR50) over its own controller-to-inverter link, which this integration cannot observe. Writes succeed and hold briefly, then revert within minutes. That's the EMS doing its job, not a failed write; taking the plant out of EMS plant mode makes manually set values stick ([#52](https://github.com/dewet22/givenergy-hass/issues/52)).
- **Wrong number of battery devices.** Battery count is auto-discovered at startup by probing the Modbus bus; there is no manual override. If detection misfires (e.g. a battery was slow to respond), reloading the integration usually fixes it. If the count is consistently wrong, [open an issue](https://github.com/dewet22/givenergy-hass/issues/48) and attach a frame capture (see [Help validate your hardware](#help-validate-your-hardware)).

For anything else, please [open an issue](https://github.com/dewet22/givenergy-hass/issues) with the relevant HA log lines and your inverter model.

## Recreating entity IDs

Occasionally an entity's id falls out of step with its name — usually a legacy slug left behind when an entity was renamed across versions (e.g. `…_grid_export_power` for what is now the **Grid Power** sensor), or a duplicate id created by removing and re-adding the integration. The friendly name is authoritative and nothing is functionally broken, so this is cosmetic — but if you'd like the ids tidied, Home Assistant can regenerate them from the device's ⋮ menu:

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/recreate-entity-ids-menu.png" alt="Recreate entity IDs in the device ⋮ menu" width="260"></p>

1. Go to **Settings → Devices & Services → GivEnergy Local** and open the device.
2. Open the **⋮** menu (top right) and choose **Recreate entity IDs**.
3. The dialog shows how many ids will be renamed versus left unchanged — choose **Update**.

<p align="center"><img src="https://raw.githubusercontent.com/dewet22/givenergy-hass/main/docs/recreate-entity-ids-confirm.png" alt="Recreate entity IDs confirmation dialog" width="480"></p>

Home Assistant keeps each entity's history and long-term statistics across the rename. What it does *not* do is update references in your own automations, scripts, scenes, or dashboards — those still point at the old ids and need editing by hand (the dialog warns about this). The integration's bundled dashboard strategy resolves entities live, so it isn't affected.

## License

Apache License 2.0 — see [LICENSE](https://github.com/dewet22/givenergy-hass/blob/main/LICENSE).

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

After cloning, run the setup script once:

```bash
./scripts/setup.sh     # managed Python + dependencies + pre-commit hook
```

This provisions the uv-managed Python pinned in `.python-version`, installs the
dev dependencies, and wires the [prek](https://prek.j178.dev) pre-commit hook.
The hook step is per-clone — git never clones `.git/hooks` — and prek must be
installed separately (e.g. `brew install prek`); if it's missing the script
prints how to install it and stops short of wiring the hook. The hooks are
local-only; CI runs HACS + hassfest, not these checks.

Day-to-day commands:

```bash
uv sync --dev          # install dependencies
uv run pytest          # run tests
uv run ruff check .    # lint
uv run mypy custom_components/  # type-check
```

The dev environment requires Python 3.14.2 or later — `pyproject.toml` pins this to match HA Core's own lock-file floor and to keep transitive Dependabot alerts (pillow, cryptography) off the resolver. The integration runtime itself only needs whatever Python 3.14.x your Home Assistant install ships with.
