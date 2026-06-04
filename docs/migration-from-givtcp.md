# Migrating from GivTCP — sensor catalogue

This document catalogues GivTCP sensors against their `givenergy_local` equivalents, with the goal of supporting users who want to migrate long-term statistics from GivTCP without losing their Energy dashboard history (see issue #67).

Suffixes shown are after stripping the integration prefix and the inverter/battery serial. For a given inverter with serial `SN`:

- GivTCP: `sensor.givtcp_<sn>_<suffix>` (or `givtcp_<battery_sn>_battery_<suffix>` for battery-pack sensors)
- givenergy_local: `sensor.givenergy_inverter_<sn>_<suffix>` (or `sensor.givenergy_battery_<battery_sn>_<suffix>` for batteries)

## Status legend

| Icon | Meaning |
|---|---|
| ✅ | **Verified pair** — same register; live values agree exactly on a reference system. Safe to migrate. |
| 🔁 | **Likely pair** — semantically equivalent and almost certainly the same register, but not yet live-verified across firmware versions. |
| ⚠️ | **Diverged** — same concept, but the underlying registers (or scaling, or reset epoch) differ. Live values disagree. Do not migrate without manual review. |
| 🚫 | **Gap** — GivTCP exposes this, `givenergy_local` does not. May need upstream decode work or a deliberate decision to drop. |
| 🆕 | **New in givenergy_local** — no GivTCP equivalent. Nothing to migrate; mentioned for completeness. |
| 🛠️ | **GivTCP-derived helper** — not a register read; computed by GivTCP. HA can derive the same itself (template sensor or built-in dashboard logic). |

## Summary

- GivTCP entities on reference system: **200**
- `givenergy_local` entities on reference system: **187**

| Status | Count |
|---|---:|
| ✅ verified | 12 |
| 🔁 likely | 41 |
| ⚠️ diverged | 1 |
| 🚫 gap | 78 |
| 🆕 new | 71 |
| 🛠️ derived givtcp | 20 |

> Counts are *suffix*-level (one row per logical sensor); on a real system each per-battery row multiplies by the number of battery packs, and per-cell rows multiply by 16. The reference system above has two battery packs (`BG2134G007`, `DZ2228G532`).

## Catalogue

### Energy dashboard (cumulative kWh)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| ✅ | `battery_charge_energy_today_kwh` | `battery_charge_today` | Battery charge today (HR(4114)) |
| ✅ | `battery_discharge_energy_today_kwh` | `battery_discharge_today` | Battery discharge today |
| ✅ | `battery_throughput_total_kwh` | `battery_throughput_total` | From IR(6)/IR(7) |
| ✅ | `export_energy_today_kwh` | `grid_export_today` | Grid export today |
| ✅ | `export_energy_total_kwh` | `grid_export_total` | Grid export lifetime |
| ✅ | `import_energy_today_kwh` | `grid_import_today` | Grid import today |
| ✅ | `import_energy_total_kwh` | `grid_import_total` | Grid import lifetime |
| ✅ | `invertor_energy_today_kwh` | `inverter_output_today` | Inverter AC output today |
| ✅ | `invertor_energy_total_kwh` | `inverter_output_total` | Inverter AC output lifetime |
| ✅ | `load_energy_today_kwh` | `house_consumption_today` | House consumption today (the integration's derived consumption — givenergy-modbus #174; the old `load_energy_today` was a mislabel that read ~0) |
| ✅ | `pv_energy_today_kwh` | `pv_energy_today` | Solar generation today |
| ✅ | `pv_energy_total_kwh` | `pv_energy_total` | Solar generation lifetime |
| ⚠️ | `ac_charge_energy_total_kwh` | `charge_from_grid_total` | Live values disagree by ~36× (25.5 kWh vs 0.7 kWh). Likely reads a different register block, or has been reset more recently. |
| 🚫 | `ac_charge_energy_today_kwh` | — | No `charge_from_grid_today` exists; only the lifetime total. |
| 🚫 | `battery_charge_energy_total_kwh` | — | givenergy_local only exposes `battery_alt_charge_total` (HR(4111-4112)), which reads a different register and is ~3× lower. Needs upstream `givenergy-modbus` work to decode the primary lifetime accumulator. |
| 🚫 | `battery_discharge_energy_total_kwh` | — | Same story as charge total — only `battery_alt_discharge_total` exists, and it reads a different register. |
| 🚫 | `battery_throughput_today_kwh` | — | Only the lifetime throughput is exposed by givenergy_local. |
| 🚫 | `load_energy_total_kwh` | — | Daily counter exists in givenergy_local; lifetime accumulator does not. |
| 🚫 | `self_consumption_energy_today_kwh` | — | GivTCP-derived (PV − export). HA Energy dashboard derives the equivalent itself. |
| 🚫 | `self_consumption_energy_total_kwh` | — | Same — GivTCP-derived. |

### Power (instantaneous W)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `battery_power` | `battery_power` | Signed in both |
| 🔁 | `combined_generation_power` | `combined_generation_power` |  |
| 🔁 | `export_power` | `grid_power` | Renamed in v1.1.x — signed net at meter, positive = export |
| 🔁 | `grid_power` | `grid_power_phase_1` | Single-phase inverter; three-phase users get three of these |
| 🔁 | `load_power` | `load_power` | House load power (same name, same concept) |
| 🔁 | `pv_power` | `pv_power` | PV total |
| 🔁 | `pv_power_string_1` | `pv_string_1_power` |  |
| 🔁 | `pv_power_string_2` | `pv_string_2_power` |  |
| 🚫 | `ac_charge_power` | — |  |
| 🛠️ | `charge_power` | — | GivTCP-derived; +ve part of `battery_power` |
| 🛠️ | `discharge_power` | — | GivTCP-derived; -ve part of `battery_power` |
| 🛠️ | `grid_power_inverted` | — | GivTCP sign-flipped helper |
| 🛠️ | `import_power` | — | GivTCP-derived from `grid_power` sign |
| 🛠️ | `self_consumption_power` | — | GivTCP-derived |

### Power-flow decomposition (GivTCP-derived)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🛠️ | `battery_to_grid` | — | All derived sensors splitting flows by source/sink |
| 🛠️ | `battery_to_house` | — |  |
| 🛠️ | `grid_to_battery` | — |  |
| 🛠️ | `grid_to_house` | — |  |
| 🛠️ | `solar_to_battery` | — |  |
| 🛠️ | `solar_to_grid` | — |  |
| 🛠️ | `solar_to_house` | — |  |

### Battery state (inverter-side)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `battery_calibration_status` | `battery_calibration_stage` | Both report calibration state but enumeration may differ |
| 🔁 | `battery_capacity_kwh` | `battery_nominal_capacity` |  |
| 🔁 | `battery_current` | `battery_current` |  |
| 🔁 | `battery_type` | `battery_type` |  |
| 🔁 | `battery_voltage` | `battery_voltage` | Inverter-side voltage (per-pack voltage is `givenergy_battery_<sn>_voltage`) |
| 🛠️ | `soc_kwh` | — | GivTCP-derived from SOC% × capacity |

### Battery state / control (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `battery_charge_energy_total_computed` | — | Auto-categorised; not yet manually mapped. Example: `sensor.battery_charge_energy_total_computed` |
| 🚫 | `battery_discharge_energy_today_kwh_negated` | — | Auto-categorised; not yet manually mapped. Example: `sensor.battery_discharge_energy_today_kwh_negated` |
| 🚫 | `battery_discharge_energy_total_computed` | — | Auto-categorised; not yet manually mapped. Example: `sensor.battery_discharge_energy_total_computed` |
| 🚫 | `soc` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_soc` |
| 🆕 | — | `battery_alt_charge_today` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_alt_charge_today` |
| 🆕 | — | `battery_alt_charge_total` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_alt_charge_total` |
| 🆕 | — | `battery_alt_discharge_today` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_alt_discharge_today` |
| 🆕 | — | `battery_alt_discharge_total` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_alt_discharge_total` |
| 🆕 | — | `battery_capacity` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_capacity` |
| 🆕 | — | `battery_discharge_min_power_reserve` | No GivTCP equivalent. Example: `number.givenergy_inverter_sa2114g047_battery_discharge_min_power_reserve` |
| 🆕 | — | `battery_discharge_this_year` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_discharge_this_year` |
| 🆕 | — | `battery_maintenance_mode` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_maintenance_mode` |
| 🆕 | — | `battery_pause_mode` | No GivTCP equivalent. Example: `select.givenergy_inverter_sa2114g047_battery_pause_mode` |
| 🆕 | — | `battery_pause_slot_end` | No GivTCP equivalent. Example: `time.givenergy_inverter_sa2114g047_battery_pause_slot_end` |
| 🆕 | — | `battery_pause_slot_start` | No GivTCP equivalent. Example: `time.givenergy_inverter_sa2114g047_battery_pause_slot_start` |
| 🆕 | — | `battery_power_mode` | No GivTCP equivalent. Example: `select.givenergy_inverter_sa2114g047_battery_power_mode` |
| 🆕 | — | `battery_soc` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_soc` |
| 🆕 | — | `battery_temperature` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_battery_temperature` |
| 🆕 | — | `charge_status` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_charge_status` |
| 🆕 | — | `restore_full_givenergy_battery_discharge_after_octopus_intelligent_dispatching` | No GivTCP equivalent. Example: `automation.restore_full_givenergy_battery_discharge_after_octopus_intelligent_dispatching` |

### PV strings

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `pv_current_string_1` | `pv_string_1_current` |  |
| 🔁 | `pv_current_string_2` | `pv_string_2_current` |  |
| 🔁 | `pv_voltage_string_1` | `pv_string_1_voltage` |  |
| 🔁 | `pv_voltage_string_2` | `pv_string_2_voltage` |  |

### AC / Grid measurements

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `grid_current` | `grid_port_current` |  |
| 🔁 | `grid_frequency` | `ac_frequency` |  |
| 🔁 | `grid_voltage` | `ac_voltage` |  |

### AC / Grid measurements (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `ac_output_current` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_ac_output_current` |
| 🆕 | — | `ac_output_frequency` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_ac_output_frequency` |
| 🆕 | — | `ac_output_voltage` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_ac_output_voltage` |
| 🆕 | — | `grid_apparent_power` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_grid_apparent_power` |
| 🆕 | — | `inverter_export_total` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_inverter_export_total` |

### Tariff (Octopus day/night)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `day_energy_kwh` | — | GivTCP-specific day-rate accumulator. Octopus integration handles tariff splitting in the givenergy_local world. |
| 🚫 | `day_energy_total_kwh` | — |  |
| 🚫 | `day_start_energy_kwh` | — |  |
| 🚫 | `night_energy_kwh` | — |  |
| 🚫 | `night_energy_total_kwh` | — |  |
| 🚫 | `night_start_energy_kwh` | — |  |

### Cost / monetary (GivTCP-specific)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `export_limit` | — | GivTCP export-limit number entity. givenergy_local doesn't expose this control. |
| 🚫 | `export_rate` | — | GivTCP export rate; no givenergy_local equivalent. |
| 🛠️ | `battery_ppkwh` | — | GivTCP price-per-kWh estimate. |
| 🛠️ | `battery_value` | — | GivTCP currency-of-stored-energy estimate. |
| 🛠️ | `import_ppkwh_today` | — | GivTCP rate snapshot. |

### Status / timing

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🛠️ | `charge_completion_time` | — | GivTCP-derived from SOC + charge rate |
| 🛠️ | `charge_time_remaining` | — |  |
| 🛠️ | `discharge_completion_time` | — |  |
| 🛠️ | `discharge_time_remaining` | — |  |

### Status / Diagnostic

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `backup_power` | EPS / EMS mode indicator. GivTCP may have surfaced this concept as `ems_status` or similar, but no confirmed equivalent was found on the reference system. |
| 🆕 | — | `consecutive_refresh_failures` | Modbus read failure counter. No GivTCP equivalent; GivTCP doesn't expose internal polling health. Worth including in LTS for long-term reliability tracking. |
| 🆕 | — | `total_refresh_failures` | Cumulative Modbus failure count — see `consecutive_refresh_failures` note. |

### Status / Diagnostic (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `invertor_serial_number` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_serial_number` |
| 🚫 | `invertor_temperature` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_temperature` |
| 🚫 | `invertor_type` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_type` |
| 🚫 | `meter_type` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_meter_type` |
| 🚫 | `modbus_version` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_modbus_version` |
| 🚫 | `reboot_invertor` | — | Auto-categorised; not yet manually mapped. Example: `button.givtcp_sa2114g047_reboot_invertor` |
| 🚫 | `status` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_status` |
| 🆕 | — | `arm_firmware_version` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_arm_firmware_version` |
| 🆕 | — | `charger_warning_code` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_charger_warning_code` |
| 🆕 | — | `device_type_code` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_device_type_code` |
| 🆕 | — | `dsp_firmware_version` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_dsp_firmware_version` |
| 🆕 | — | `fault_code` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_fault_code` |
| 🆕 | — | `fault_messages` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_fault_messages` |
| 🆕 | — | `inverter_errors` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_inverter_errors` |
| 🆕 | — | `inverter_heatsink_temperature` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_inverter_heatsink_temperature` |
| 🆕 | — | `last_successful_refresh` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_last_successful_refresh` |
| 🆕 | — | `meter_type` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_meter_type` |
| 🆕 | — | `modbus_version` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_modbus_version` |
| 🆕 | — | `mppt_count` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_mppt_count` |
| 🆕 | — | `negative_dc_bus_voltage` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_negative_dc_bus_voltage` |
| 🆕 | — | `phase_count` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_phase_count` |
| 🆕 | — | `positive_dc_bus_voltage` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_positive_dc_bus_voltage` |
| 🆕 | — | `status` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_status` |
| 🆕 | — | `system_mode` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_system_mode` |
| 🆕 | — | `usb_device` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_usb_device` |

### Controls — number entities

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `battery_charge_rate` | `battery_charge_limit` | AC power cap as % of inverter rating |
| 🔁 | `battery_discharge_rate` | `battery_discharge_limit` |  |
| 🔁 | `battery_power_reserve` | `battery_soc_reserve` | Identical concept (% SOC reserve) |
| 🔁 | `target_soc` | `charge_target_soc` | Charge target SoC (%) |
| 🚫 | `battery_power_cutoff` | — | GivTCP-specific safety floor; givenergy_local has `battery_discharge_min_power_reserve` which may be equivalent — needs verification |
| 🚫 | `force_charge_num` | — |  |
| 🚫 | `force_export_num` | — |  |
| 🚫 | `temp_pause_charge_num` | — | givenergy_local uses `battery_pause_mode` select + pause-slot times instead |
| 🚫 | `temp_pause_discharge_num` | — |  |

### Controls — select entities

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `battery_calibration` | — | Different mechanism in givenergy_local |
| 🚫 | `force_charge` | — |  |
| 🚫 | `force_export` | — |  |
| 🚫 | `temp_pause_charge` | — | Replaced by `battery_pause_mode` (single select with four options) |
| 🚫 | `temp_pause_discharge` | — |  |

### Controls / schedule (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `enable_charge` | No GivTCP equivalent. Example: `switch.givenergy_inverter_sa2114g047_enable_charge` |
| 🆕 | — | `enable_discharge` | No GivTCP equivalent. Example: `switch.givenergy_inverter_sa2114g047_enable_discharge` |

### Switches

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `enable_charge_schedule` | — | givenergy_local has `enable_charge` (single switch, not per-slot) |
| 🚫 | `enable_charge_target` | — |  |
| 🚫 | `enable_discharge_schedule` | — |  |

### Time slots

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `charge_end_time_slot_1` | `charge_slot_1_end` |  |
| 🔁 | `charge_start_time_slot_1` | `charge_slot_1_start` | GivTCP uses `select`; givenergy_local uses native `time` entity |
| 🔁 | `discharge_end_time_slot_1` | `discharge_slot_1_end` |  |
| 🔁 | `discharge_end_time_slot_2` | `discharge_slot_2_end` |  |
| 🔁 | `discharge_start_time_slot_1` | `discharge_slot_1_start` |  |
| 🔁 | `discharge_start_time_slot_2` | `discharge_slot_2_start` |  |

### Time slots (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `invertor_time` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_time` |
| 🚫 | `last_updated_time` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_last_updated_time` |
| 🚫 | `real_time_control` | — | Auto-categorised; not yet manually mapped. Example: `switch.givtcp_sa2114g047_real_time_control` |
| 🚫 | `sync_time` | — | Auto-categorised; not yet manually mapped. Example: `button.givtcp_sa2114g047_sync_time` |
| 🚫 | `time_since_last_update` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_time_since_last_update` |
| 🚫 | `timeout_error` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_timeout_error` |
| 🆕 | — | `charge_slot_2_end` | No GivTCP equivalent. Example: `time.givenergy_inverter_sa2114g047_charge_slot_2_end` |
| 🆕 | — | `charge_slot_2_start` | No GivTCP equivalent. Example: `time.givenergy_inverter_sa2114g047_charge_slot_2_start` |
| 🆕 | — | `work_time_total` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_work_time_total` |

### Per-battery (BMS / cells)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🔁 | `battery_capacity` | `calibrated_capacity` |  |
| 🔁 | `battery_cell_N_temperature` | `cells_N_M_temperature` | GivTCP exposes per-cell temps for cells 1-4 only; givenergy_local groups them as `cells_1_4_temperature`, `cells_5_8_temperature`, `cells_9_12_temperature`, `cells_13_16_temperature` |
| 🔁 | `battery_cell_N_voltage` | `cell_N_voltage` | N = 1..16 (or 1..cell_count) |
| 🔁 | `battery_cells` | `cell_count` |  |
| 🔁 | `battery_cycles` | `charge_cycles` |  |
| 🔁 | `battery_design_capacity` | `design_capacity` |  |
| 🔁 | `battery_firmware_version` | `bms_firmware_version` |  |
| 🔁 | `battery_remaining_capacity` | `remaining_capacity` |  |
| 🔁 | `battery_soc` | `soc` | Per-pack SOC |
| 🔁 | `battery_temperature` | `temperature_max` | GivTCP reports a single temp; givenergy_local exposes both min and max |
| 🔁 | `battery_voltage` | `voltage` | Per-pack voltage |
| 🚫 | `battery_serial_number` | — | Encoded in entity ID; no separate sensor needed |
| 🚫 | `battery_stack_1_bms_temperature` | — | GivTCP exposes a stack-level temperature; givenergy_local uses per-pack instead |
| 🚫 | `battery_stack_1_bms_voltage` | — |  |

### Per-battery (BMS / cells) (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `bms_mosfet_temperature` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_mosfet_temperature` |
| 🆕 | — | `bms_status_1` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_1` |
| 🆕 | — | `bms_status_2` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_2` |
| 🆕 | — | `bms_status_3` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_3` |
| 🆕 | — | `bms_status_4` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_4` |
| 🆕 | — | `bms_status_5` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_5` |
| 🆕 | — | `bms_status_6` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_6` |
| 🆕 | — | `bms_status_7` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_status_7` |
| 🆕 | — | `bms_warning_1` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_warning_1` |
| 🆕 | — | `bms_warning_2` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_bms_warning_2` |
| 🆕 | — | `cell_voltages_sum` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_cell_voltages_sum` |

### Connectivity (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `givenergy_local_update` | No GivTCP equivalent. Example: `update.givenergy_local_update` |
| 🆕 | — | `rx` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_rx` |
| 🆕 | — | `tx` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_tx` |

### Energy dashboard (cumulative kWh) (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `inverter_output_frequency` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_inverter_output_frequency` |

### PV strings (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🆕 | — | `pv_string_1_energy_today` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_pv_string_1_energy_today` |
| 🆕 | — | `pv_string_2_energy_today` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_pv_string_2_energy_today` |
| 🆕 | — | `solar_diverter_energy_total` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_solar_diverter_energy_total` |

### Other (uncategorised)

| Status | GivTCP suffix | `givenergy_local` suffix | Notes |
|---|---|---|---|
| 🚫 | `active_power_rate` | — | Auto-categorised; not yet manually mapped. Example: `number.givtcp_sa2114g047_active_power_rate` |
| 🚫 | `charge_energy` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givenergy_battery_charge_energy` |
| 🚫 | `cpu_percent` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_cpu_percent` |
| 🚫 | `current_rate` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_current_rate` |
| 🚫 | `current_rate_type` | — | Auto-categorised; not yet manually mapped. Example: `select.givtcp_sa2114g047_current_rate_type` |
| 🚫 | `day_cost` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_day_cost` |
| 🚫 | `day_rate` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_day_rate` |
| 🚫 | `disharge_energy` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givenergy_battery_disharge_energy` |
| 🚫 | `eco_mode` | — | Auto-categorised; not yet manually mapped. Example: `switch.givtcp_sa2114g047_eco_mode` |
| 🚫 | `energy_battery_givtcp_sa2114g047_discharge_power_givtcp_sa2114g047_charge_power_net_power` | — | Auto-categorised; not yet manually mapped. Example: `sensor.energy_battery_givtcp_sa2114g047_discharge_power_givtcp_sa2114g047_charge_power_net_power` |
| 🚫 | `eps_energy` | — | Auto-categorised; not yet manually mapped. Example: `sensor.eps_energy` |
| 🚫 | `eps_power` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_eps_power` |
| 🚫 | `givtcp_version` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_givtcp_version` |
| 🚫 | `invertor_firmware` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_firmware` |
| 🚫 | `invertor_max_bat_rate` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_max_bat_rate` |
| 🚫 | `invertor_max_inv_rate` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_max_inv_rate` |
| 🚫 | `invertor_power` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_invertor_power` |
| 🚫 | `memory_percent` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_memory_percent` |
| 🚫 | `mode` | — | Auto-categorised; not yet manually mapped. Example: `select.givtcp_sa2114g047_mode` |
| 🚫 | `newest_version` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_newest_version` |
| 🚫 | `night_cost` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_night_cost` |
| 🚫 | `night_rate` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_night_rate` |
| 🚫 | `reboot_addon` | — | Auto-categorised; not yet manually mapped. Example: `button.givtcp_sa2114g047_reboot_addon` |
| 🚫 | `restart_givtcp_if_borked_for_5m` | — | Auto-categorised; not yet manually mapped. Example: `automation.restart_givtcp_if_borked_for_5m` |
| 🚫 | `running` | — | Auto-categorised; not yet manually mapped. Example: `binary_sensor.givtcp_running` |
| 🚫 | `safe_write_count` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_safe_write_count` |
| 🚫 | `version` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_version` |
| 🚫 | `write_count` | — | Auto-categorised; not yet manually mapped. Example: `sensor.givtcp_sa2114g047_write_count` |
| 🆕 | — | — | No GivTCP equivalent. Example: `device_tracker.givenergy_inverter` |
| 🆕 | — | `charge_energy` | No GivTCP equivalent. Example: `sensor.givenergy_battery_charge_energy` |
| 🆕 | — | `charger_temperature` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_charger_temperature` |
| 🆕 | — | `design_capacity_alt` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_design_capacity_alt` |
| 🆕 | — | `disharge_energy` | No GivTCP equivalent. Example: `sensor.givenergy_battery_disharge_energy` |
| 🆕 | — | `inverter_power_factor` | No GivTCP equivalent. Example: `sensor.givenergy_inverter_sa2114g047_inverter_power_factor` |
| 🆕 | — | `temperature_min` | No GivTCP equivalent. Example: `sensor.givenergy_battery_bg2134g007_temperature_min` |

## Migration design notes

Referenced by issue #67. Captured here so the design space stays in-repo.

### Mechanism

Home Assistant's long-term statistics live in the recorder DB (SQLite/MariaDB/Postgres), in `statistics_meta` (one row per `statistic_id`) and `statistics` / `statistics_short_term` (rows keyed by `metadata_id`). Migration is fundamentally a re-pointing of `metadata_id` from old GivTCP rows to the new `givenergy_local` `statistic_id`.

### Things to get right

1. **Sum-column rebase.** For `total_increasing` energy sensors, HA stores both `state` (instantaneous meter reading) and `sum` (cumulative integral, with monotonicity resets). Naively swapping `metadata_id` produces a visual cliff in the Energy dashboard. The migration must rebase the new sensor's `sum` so it continues from where the old one left off.
2. **Unit alignment.** All verified pairs above use `kWh`. Older GivTCP versions reported some sensors in `Wh` — the migration tool must check `statistics_meta.unit_of_measurement` and either match or scale.
3. **Backend coverage.** The schema is identical across SQLite/MariaDB/Postgres but quoting and transaction handling differ.
4. **Overlap handling.** If both integrations were running in parallel during a cutover window, the user needs to choose a rule: prefer-old, prefer-new, or refuse-and-flag.
5. **Reversibility.** Backup the recorder DB before any write. Dry-run mode that prints the planned diff is mandatory.
6. **Multi-inverter / multi-battery.** Iterate per serial; serials are extracted from existing `statistic_id`s, not hard-coded.
7. **Firmware-aware register order.** Per `givenergy-modbus/model/gateway.py`, AIO gateway energy-total registers swap high/low order between GA000009 and GA000010 firmware. Direct register-level migrations need to know which gateway firmware the user has — the entity-level approach used here side-steps this because both integrations already account for it.

### `battery_*_total_kwh` (charge/discharge) — open question

The two `🚫 gap` rows for `battery_charge_energy_total_kwh` and `battery_discharge_energy_total_kwh` are the most consequential: these are the lifetime accumulators that many users will have years of history for. `givenergy_local` only exposes the daily counter (from `HR(4114)` / `HR(4113)`) and a separate `*_alt_total` (from `HR(4109-4112)`) which reads a different register block and produces values 3-6× lower than GivTCP's lifetime totals on the reference system.

Three viable paths:

- **(a) Leave GivTCP history under a renamed `statistic_id`** — e.g. `sensor.battery_charge_total_legacy` — and reference it in the Energy dashboard separately. No new register decode needed.
- **(b) Add the missing register decode upstream** in `givenergy-modbus` so `givenergy_local` can ship a proper `Battery Charge Total` sensor that reads the same register GivTCP does. Then migrate normally.
- **(c) Document the limitation** and let users decide per-entity.

Option (b) is the cleanest long-term answer but depends on upstream work.
