# Release notes

Human-readable notes for each release. The authoritative, machine-readable record
is the set of annotated git tags (`git show v1.1.0`); this file mirrors them in one
place. For releases prior to v1.1.0, see the
[GitHub Releases](https://github.com/dewet22/givenergy-hass/releases) page.

---

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
