# GivEnergy Local Home Assistant custom component

A Home Assistant custom integration for GivEnergy plants (combination of inverter, solar/photovoltaic and batteries) which communicates directly over local Modbus TCP — no cloud, no GivEnergy portal account required.

Uses [`givenergy-modbus`](https://github.com/dewet22/givenergy-modbus) for the underlying communication and state management.

## Requirements

- A [supported GivEnergy inverter](#supported-inverters) connected to your local network (wifi or ethernet), with the Modbus TCP port reachable from your Home Assistant server (default port **8899**)
- Home Assistant 2025.4 or later

## Supported inverters

The underlying [`givenergy-modbus`](https://github.com/dewet22/givenergy-modbus) library knows the following inverter families: Hybrid (1ph/3ph), AC, EMS, Gateway, and All-in-One. However, **this integration has only been tested by the author on a Hybrid Gen 1**. If you have a different model and would like to help validate the integration against it, please [open an issue](https://github.com/dewet22/givenergy-hass/issues) — bug reports, register dumps, and PRs are all very welcome.

Older Gen 2 units with the `EA` serial prefix are not currently supported by the underlying library — again, any owners willing to help test are very welcome!

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dewet22&repository=givenergy-hass&category=integration)

Or if that doesn't work:

1. In HACS, go to **Integrations → Custom repositories**
2. Add `https://github.com/dewet22/givenergy-hass` and select category **Integration**
3. Install **GivEnergy Local** and restart Home Assistant

### Manual

1. Download [`givenergy_local.zip`](https://github.com/dewet22/givenergy-hass/releases/latest/download/givenergy_local.zip) from the latest release
2. Extract its contents into your Home Assistant `config/custom_components/givenergy_local/` folder
3. Restart Home Assistant

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → GivEnergy Local**.

| Field | Default | Description |
|---|---|---|
| Inverter IP Address | — | Local IP of the inverter's data adapter |
| Modbus Port | `8899` | Modbus TCP port |
| Scan Interval | `30` s | How often HA polls for updated values |
| Number of Batteries | `1` | Number of battery units connected |
| [Passive mode](#passive-mode) | off | Listen only — use when another Modbus client (e.g. the GivEnergy app) is already polling and this integration should just observe |

To change any of these later, open the integration's **⋮** menu in **Settings → Devices & Services → GivEnergy Local** and choose **Reconfigure**. The integration reloads automatically when you save.

### Passive mode

When enabled, the integration connects to the inverter but does not send any Modbus read requests after the initial connection. Instead, it reads the library's register cache on each scan interval tick. This is useful when you have another client (e.g. GivTCP or a mobile app) already polling the inverter — having multiple clients requesting large register bank reads tend to get the inverter confused by stepping on each other. This is also useful if you are migrating from GivTCP and want to keep both running for the time being.

## Entities

### Inverter device

#### Sensors

| Entity | Unit | Notes |
|---|---|---|
| PV Power | W | Combined PV output |
| PV String 1 / 2 Power | W | Per-string power |
| PV String 1 / 2 Voltage | V | |
| PV String 1 / 2 Current | A | |
| PV Energy Today | kWh | |
| PV Energy Total | kWh | |
| Battery SOC | % | |
| Battery Power | W | Positive = discharging, negative = charging |
| Battery Voltage / Current | V / A | |
| Battery Temperature | °C | |
| Battery Charge Today | kWh | |
| Battery Discharge Today | kWh | |
| Battery Throughput Total | kWh | |
| Grid Export Power | W | Positive = exporting, negative = importing |
| Grid Export / Import Today | kWh | |
| Grid Export / Import Total | kWh | |
| AC Voltage / Frequency | V / Hz | |
| Load Power | W | |
| Load Energy Today | kWh | |
| Inverter Output Today / Total | kWh | |
| Inverter Heatsink Temperature | °C | |
| Charger Temperature | °C | |
| Status | — | e.g. Normal, Warning, Fault |
| Fault Code | — | |
| Inverter Errors | — | Diagnostic; error bitmask |
| Charger Warning Code | — | Diagnostic |
| Charge Status | — | Diagnostic; raw int (BMS state code, mapping TBD) |
| System Mode | — | Diagnostic; raw int (operating mode, mapping TBD) |
| Battery Pause Mode | — | Diagnostic; raw int (pause-charging state) |
| AC Output Voltage / Frequency / Current | V / Hz / A | Diagnostic; inverter output (post-conversion) |
| Grid Apparent Power | VA | Diagnostic |
| Inverter Power Factor | — | Diagnostic |
| Grid Power Phase 1 | W | Diagnostic; useful for 3-phase models |
| Inverter Export Total | kWh | Cumulative inverter export to grid |
| Charge from Grid Total | kWh | Cumulative grid-sourced battery charging |
| Battery Discharge This Year | kWh | |
| Backup Power | W | EPS port output |
| Combined Generation Power | W | Solar + battery combined |
| Work Time Total | h | |
| Device Type Code | — | Diagnostic |
| MPPT Count | — | Diagnostic |
| Phase Count | — | Diagnostic; 1 for single-phase, 3 for three-phase |
| ARM / DSP / Modbus Firmware Version | — | Diagnostic |
| Meter Type | — | Diagnostic; CT-or-EM418 / EM115 |
| Battery Type | — | Diagnostic; Lithium / Lead-Acid |
| Battery Capacity | Ah | Diagnostic; reported pack capacity |
| Battery Nominal Capacity | kWh | Diagnostic; computed from Ah × nominal voltage |
| Last Successful Refresh | timestamp | Diagnostic |
| Consecutive Refresh Failures | — | Diagnostic; resets to 0 on next success |
| Total Refresh Failures | — | Diagnostic; ever-increasing counter (resets only when HA restarts — HA's long-term statistics handle that transparently) |

#### Controls

| Entity | Type | Notes |
|---|---|---|
| Enable Charge | Switch | |
| Enable Discharge | Switch | |
| Charge Target SOC | Number | 4–100 % |
| Battery SOC Reserve | Number | 4–100 % |
| Battery Charge Limit | Number | 0–50 % |
| Battery Discharge Limit | Number | 0–50 % |
| Battery Discharge Min Power Reserve | Number | 4–100 % |
| Battery Power Mode | Select | Export / Self Consumption |
| Charge Slot 1 & 2 Start / End | Time | |
| Discharge Slot 1 & 2 Start / End | Time | |

### Battery device(s)

Each battery appears as a separate device linked to the inverter.

| Entity | Unit | Notes |
|---|---|---|
| SOC | % | |
| Voltage | V | Pack output voltage |
| Temperature Max / Min | °C | |
| Remaining Capacity | Ah | |
| Design Capacity | Ah | |
| Charge Cycles | — | |
| Cell Count | — | Diagnostic; number of cells the BMS reports |
| Cell Voltages Sum | V | Diagnostic; sanity-check against Voltage |
| BMS MOSFET Temperature | °C | Diagnostic |
| Cell 1 … 16 Voltage | V | Diagnostic; per-cell. Unused positions in smaller packs read ~0 |
| Cells 1-4 / 5-8 / 9-12 / 13-16 Temperature | °C | Diagnostic; the BMS samples one thermistor per 4-cell group |

Cell-level entities are tagged as diagnostic, so they're hidden from the default device view but available for dashboards and pack-health monitoring (cell voltage spread, temperature deltas, etc.).

### Not exposed by default

The upstream library makes ~180 inverter fields available; this integration intentionally exposes the subset that's useful for end users without being unsafe or noisy. Deliberately skipped for now:

- `enable_*` flags for low-level inverter behaviour (buzzer, RTC, BMS read, frequency derating, auto-judge battery type, …) — changing these from a UI toggle is rarely what you actually want
- Battery calibration registers, voltage-adjust trims, low-voltage force-charge timers
- Charge / discharge slots 3 - 10 and their per-slot SOC stops (slots 1 and 2 cover typical Eco/Timed usage)
- Admin / destructive actions: inverter reboot, BMS flash update, auto-test triggers, ARM-chip select, user-code register
- Raw debug fields (internal bus voltages, countdown timers, `debug_inverter`)
- Per-phase three-phase data beyond `Grid Power Phase 1` and the three-phase balance registers

If any of these would genuinely help your setup, [open an issue](https://github.com/dewet22/givenergy-hass/issues) describing the use case — the field probably can be exposed with a single description entry, but it's nicer to have a concrete reason to do it. The same applies if a sensor we *do* expose looks wrong on your inverter — **real-world testing on non-Hybrid Gen 1 hardware (AC, AC3, EMS, Gateway, All-in-One) is especially appreciated**, and a register dump from your unit goes a long way.

## Energy dashboard

All cumulative-energy entities (kWh) are exposed with `device_class=energy` and `state_class=total_increasing`, so Home Assistant generates long-term statistics for them automatically and they show up directly in the Energy dashboard's entity picker.

### Required: energy sensors (kWh, for the dashboard graphs)

| Dashboard slot | Entity |
|---|---|
| Solar production | `PV Energy Today` (or per-string `PV String 1/2 Energy Today` if you'd rather track MPPTs individually) |
| Grid consumption | `Grid Import Today` |
| Return to grid | `Grid Export Today` |
| Home battery — energy going IN | `Battery Charge Today` |
| Home battery — energy coming OUT | `Battery Discharge Today` |

The dashboard derives household consumption automatically from the above. If you'd like to track it directly as a sanity check, `Load Energy Today` measures the total household demand fed by the inverter and can be added under "Individual devices".

### Optional: power sensors (W, for the "Now" live view)

The dashboard's live view shows current power flow between solar, grid, battery and load. Wire these in once the energy mappings above are in place:

| Dashboard slot | Entity | Sign convention |
|---|---|---|
| Solar power | `PV Power` | Positive when producing |
| Grid power | `Grid Export Power` | Positive = exporting, negative = importing |
| Battery power | `Battery Power` | Positive = discharging, negative = charging |
| Household demand | `Load Power` | This would be universally positive, unless you have another generation source |

The daily counters reset at midnight; Home Assistant's recorder detects the reset automatically thanks to the `total_increasing` state class, so deltas across day boundaries are accounted for correctly.

## Troubleshooting

- **Transient connection drops are normal.** TCP-level timeouts and the occasional connection reset get logged at WARNING level and the next scan tick re-establishes the connection. The `Last Successful Refresh` and `Consecutive Refresh Failures` diagnostic sensors will tell you if something more persistent is going on.
- **"Register cache unchanged" failures in passive mode** mean no peer client is refreshing the inverter. Switch back to active mode, or start the other client that's supposed to be driving the bus.
- **Conflicts with another Modbus client** (GivTCP, the GivEnergy app, etc.) — the inverter doesn't always cope well with two clients issuing large reads concurrently. Use [passive mode](#passive-mode).

For anything else, please [open an issue](https://github.com/dewet22/givenergy-hass/issues) with the relevant HA log lines and your inverter model.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --dev          # install dependencies
uv run pytest          # run tests
uv run ruff check .    # lint
uv run mypy custom_components/  # type-check
```
