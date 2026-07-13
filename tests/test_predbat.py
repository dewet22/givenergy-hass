"""Tests for the Predbat apps.yaml generator (#289)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.givenergy_local.predbat import generate_apps_yaml


def _ent(serial: str, key: str, device_id: str, *, domain: str = "sensor", disabled: bool = False):
    return SimpleNamespace(
        entity_id=f"{domain}.giv_{serial.lower()}_{key}",
        platform="givenergy_local",
        device_id=device_id,
        unique_id=f"{serial}_{key}",
        disabled_by="user" if disabled else None,
    )


def _dev(device_id: str, serial: str, *, via: str | None = None):
    return SimpleNamespace(
        id=device_id, identifiers={("givenergy_local", serial)}, via_device_id=via
    )


def _entities(serial: str, keys: list[str], device_id: str):
    return [_ent(serial, k, device_id) for k in keys]


# Marker + mapped keys per role (subset sufficient for the generator).
_HYBRID_INV_KEYS = [
    "p_pv",  # inverter marker
    "battery_soc",
    "soc_kwh",
    "battery_capacity_kwh",
    "battery_soc_reserve",
    "charge_slot_1_start",
    "charge_slot_1_end",
    "charge_target_soc",
    "discharge_slot_1_start",
    "discharge_slot_1_end",
    "system_time",
    "p_battery",
    "grid_power",
    "p_load_demand",
    "e_consumption_today",
    "e_grid_in_day",
    "e_grid_out_day",
    "e_pv_day",
]
_BAT_KEYS = ["soc", "num_cycles", "v_cell_01"]
_AC_EXTRA_KEYS = ["battery_charge_limit_ac", "battery_discharge_limit_ac"]
_EMS_KEYS = [
    "ems_plant_enable",  # ems marker
    "battery_soc",
    "battery_capacity_kwh",
    "p_pv",
    "e_pv_day",
    "e_grid_in_day",
    "e_grid_out_day",
    "ems_remaining_battery_energy",
    "ems_total_battery_power",
    "ems_grid_meter_power",
    "ems_calc_load_power",
    "ems_calc_load_energy_today",
    "ems_charge_slot_1_start",
    "ems_charge_slot_1_end",
    "ems_charge_target_soc_1",
    "ems_export_slot_1_start",
    "ems_export_slot_1_end",
]


def test_single_hybrid_maps_controls_and_telemetry():
    ents = _entities("INV1", _HYBRID_INV_KEYS, "dev_inv") + _entities("BAT1", _BAT_KEYS, "dev_bat")
    devs = [_dev("dev_inv", "INV1"), _dev("dev_bat", "BAT1", via="dev_inv")]

    out = generate_apps_yaml(ents, devs)

    assert "num_inverters: 1" in out
    assert "inverter_type: ['GE']" in out
    # controls + telemetry wired to the real (key-encoded) entity ids
    assert "giv_inv1_battery_soc_reserve" in out
    assert "giv_inv1_charge_slot_1_start" in out
    assert "giv_inv1_charge_target_soc" in out
    assert "giv_inv1_system_time" in out
    assert "giv_inv1_e_grid_in_day" in out
    # soc% from the inverter's battery_soc; soc energy from soc_kwh
    assert "giv_inv1_battery_soc" in out
    assert "giv_inv1_soc_kwh" in out
    # a pure hybrid has no AC pair -> rate lines auto-omit as comments
    assert "# charge_rate_percent:" in out
    assert "# discharge_rate_percent:" in out


def test_ac_coupled_wires_rate_to_ac_pair_and_flags_pv():
    keys = _HYBRID_INV_KEYS + _AC_EXTRA_KEYS
    ents = _entities("AC1", keys, "dev_inv") + _entities("BAT1", _BAT_KEYS, "dev_bat")
    devs = [_dev("dev_inv", "AC1"), _dev("dev_bat", "BAT1", via="dev_inv")]

    out = generate_apps_yaml(ents, devs)

    # rate% maps to the AC pair (HR313/314)
    assert "charge_rate_percent:\n  - sensor.giv_ac1_battery_charge_limit_ac" in out
    assert "discharge_rate_percent:\n  - sensor.giv_ac1_battery_discharge_limit_ac" in out
    # PV is not wired from the AC unit; the user is pointed at their PV inverter
    assert "AC-coupled inverter has no PV of its own" in out
    assert "sensor.giv_ac1_e_pv_day" not in out  # not wired


def test_ems_uses_controller_and_omits_rate_and_reserve():
    ents = _entities("EMS1", _EMS_KEYS, "dev_ems")
    devs = [_dev("dev_ems", "EMS1")]

    out = generate_apps_yaml(ents, devs)

    assert "num_inverters: 1" in out
    assert "inverter_type: ['GEE']" in out
    # controls come off the EMS controller's ems_* set
    assert "charge_start_time:\n  - sensor.giv_ems1_ems_charge_slot_1_start" in out
    assert "charge_limit:\n  - sensor.giv_ems1_ems_charge_target_soc_1" in out
    assert "discharge_start_time:\n  - sensor.giv_ems1_ems_export_slot_1_start" in out
    assert "giv_ems1_ems_calc_load_energy_today" in out
    assert "giv_ems1_ems_remaining_battery_energy" in out  # soc_kw
    # rate + reserve are unmappable on EMS -> explanatory comments, not dead entities
    assert "# reserve: the EMS controller has no SOC-reserve register" in out
    assert "# charge_rate_percent: the EMS controller has no charge-rate register" in out
    assert "battery_soc_reserve" not in out
    # the portal slot-setup learning is surfaced
    assert "00:00-23:59" in out


def test_disabled_entity_is_omitted():
    keys = [k for k in _HYBRID_INV_KEYS if k != "charge_target_soc"]
    ents = _entities("INV1", keys, "dev_inv")
    ents.append(_ent("INV1", "charge_target_soc", "dev_inv", disabled=True))
    ents += _entities("BAT1", _BAT_KEYS, "dev_bat")
    devs = [_dev("dev_inv", "INV1"), _dev("dev_bat", "BAT1", via="dev_inv")]

    out = generate_apps_yaml(ents, devs)

    assert "giv_inv1_charge_target_soc" not in out
    assert "# charge_limit:" in out  # omitted with a comment


def test_no_givenergy_plant_returns_note():
    out = generate_apps_yaml([], [])
    assert "No supported single-plant topology found" in out
