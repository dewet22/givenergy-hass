# GivEnergy Local

A Home Assistant custom integration for GivEnergy hybrid inverters and batteries, communicating directly over local Modbus TCP — no cloud, no GivEnergy portal account required.

Built on top of the [`givenergy-modbus`](https://github.com/dewet22/givenergy-modbus) Python library.

## Requirements

- A GivEnergy inverter with the Modbus TCP port reachable on your local network (default port **8899**)
- Home Assistant 2024.1 or later

## Installation

### HACS (recommended)

1. In HACS, go to **Integrations → Custom repositories**
2. Add `https://github.com/dewet22/givenergy-hass` and select category **Integration**
3. Install **GivEnergy Local** and restart Home Assistant

### Manual

Copy the `custom_components/givenergy_local/` directory into your HA `config/custom_components/` folder and restart.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → GivEnergy Local**.

| Field | Default | Description |
|---|---|---|
| Inverter IP Address | — | Local IP of the inverter's data adapter |
| Modbus Port | `8899` | Modbus TCP port |
| Scan Interval | `30` s | How often HA polls for updated values |
| Number of Batteries | `1` | Number of battery units connected |
| Passive mode | off | Listen only — use when another Modbus client (e.g. the GivEnergy app) is already polling and this integration should just observe |

### Passive mode

When enabled, the integration connects to the inverter but does not send any Modbus read requests after the initial connection. Instead, it reads the library's register cache on each scan interval tick. This is useful when you have another client (e.g. the GivEnergy MQTT bridge or the official app) already polling the inverter and you don't want two clients issuing requests simultaneously.

## Entities

### Inverter device

**Sensors**

| Entity | Unit | Notes |
|---|---|---|
| PV Power | W | Combined PV output |
| PV String 1 / 2 Power | W | Per-string power |
| PV String 1 / 2 Voltage | V | |
| PV String 1 / 2 Current | A | |
| PV Energy Today | kWh | |
| PV Energy Total | kWh | |
| Battery SOC | % | |
| Battery Power | W | Positive = charging, negative = discharging |
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
| Work Time Total | h | |
| Last Successful Refresh | timestamp | Diagnostic |
| Consecutive Refresh Failures | — | Diagnostic; resets to 0 on next success |

**Controls**

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

| Entity | Unit |
|---|---|
| Battery SOC | % |
| Voltage | V |
| Temperature Max / Min | °C |
| Remaining Capacity | Ah |
| Design Capacity | Ah |
| Charge Cycles | — |

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --dev          # install dependencies
uv run pytest          # run tests
uv run ruff check .    # lint
uv run mypy custom_components/  # type-check
```
