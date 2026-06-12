// Synthetic `hass` for the dashboard-strategy tests. Its `callWS` answers the
// two registry list commands the strategy issues, built from a compact plant
// description so each test can vary topology (batteries, EMS, AC-coupled,
// smart-load) and inject an area prefix on entity_ids to prove resolution is by
// unique_id, not by a constructed entity_id string.

const INVERTER_KEYS = [
  // status / glance / power-flow
  "status", "battery_soc", "battery_pause_mode", "t_battery", "battery_out_of_spec",
  "e_pv_day", "e_battery_charge_day", "e_battery_discharge_day", "e_grid_in_day",
  "e_grid_out_day", "e_consumption_today", "p_pv", "p_battery", "grid_power",
  "p_load_demand",
  // energy totals
  "e_pv_total", "e_battery_throughput", "e_battery_charge_total",
  "e_battery_discharge_total", "e_grid_out_total", "e_grid_in_total",
  "e_pv_generation_total", "e_inverter_in_total", "e_discharge_year",
  "e_solar_diverter",
  // controls
  "battery_power_mode", "enable_rtc", "active_power_rate", "battery_calibration_stage",
  "enable_charge", "charge_target_soc", "battery_soc_reserve", "battery_charge_limit",
  "charge_slot_1_start", "charge_slot_1_end", "charge_slot_2_start", "charge_slot_2_end",
  "enable_discharge", "battery_discharge_limit", "battery_discharge_min_power_reserve",
  "discharge_slot_1_start", "discharge_slot_1_end", "discharge_slot_2_start",
  "discharge_slot_2_end",
  // diagnostics
  "last_successful_refresh", "consecutive_failures", "partial_failures", "total_failures",
  "fault_code", "inverter_fault_messages", "inverter_errors", "charger_warning_code",
  "charge_status", "system_mode", "t_inverter_heatsink", "t_charger", "v_ac1", "f_ac1",
  "v_ac1_output", "f_ac1_output", "i_ac1", "v_battery", "i_battery", "i_grid_port",
  "v_p_bus", "v_n_bus", "p_grid_apparent", "pf_inverter_output_now", "p_grid_out_ph1",
  "p_backup", "p_combined_generation", "v_pv1", "i_pv1", "p_pv1", "v_pv2", "i_pv2",
  "p_pv2", "e_pv1_day", "e_pv2_day", "battery_maintenance_mode", "arm_firmware_version",
  "dsp_firmware_version", "modbus_version", "work_time_total", "device_type_code",
  "num_mppt", "num_phases", "battery_type", "meter_type", "usb_device_inserted",
  "battery_capacity_kwh", "battery_capacity_ah",
  // money sensors (present only when tariff options are configured; tests that
  // exercise their absence pass them via omitKeys)
  "grid_import_cost_today", "grid_export_earnings_today", "net_energy_cost_today",
  "counterfactual_cost_today",
];

const BATTERY_KEYS = (function () {
  const keys = [
    "soc", "v_out", "t_max", "t_min", "t_bms_mosfet", "num_cycles", "cap_remaining",
    "cap_calibrated", "cap_design", "v_cells_sum", "num_cells", "t_cells_01_04",
    "t_cells_05_08", "t_cells_09_12", "t_cells_13_16", "bms_firmware_version",
    "usb_device_inserted", "cap_design2", "warning_1", "warning_2",
  ];
  for (let s = 1; s <= 7; s++) keys.push("status_" + s);
  for (let c = 1; c <= 16; c++) keys.push("v_cell_" + (c < 10 ? "0" + c : "" + c));
  return keys;
})();

function emsKeys() {
  const keys = ["ems_plant_enable"];
  ["charge", "discharge", "export"].forEach(function (kind) {
    for (let i = 1; i <= 3; i++) {
      keys.push("ems_" + kind + "_slot_" + i + "_start");
      keys.push("ems_" + kind + "_slot_" + i + "_end");
      keys.push("ems_" + kind + "_target_soc_" + i);
    }
  });
  // EMS controller also carries the coordinator health sensors
  return keys.concat([
    "last_successful_refresh", "consecutive_failures", "partial_failures", "total_failures",
  ]);
}

const SMART_LOAD_KEYS = (function () {
  const keys = [];
  for (let i = 1; i <= 10; i++) {
    keys.push("smart_load_slot_" + i + "_start");
    keys.push("smart_load_slot_" + i + "_end");
  }
  return keys;
})();

const AC_COUPLED_KEYS = [
  "export_priority", "enable_eps", "battery_charge_limit_ac", "battery_discharge_limit_ac",
];

// entity_id marker: includes the (optional) area prefix so tests can assert the
// returned config used the registry's *current* id, not a reconstructed one.
function entityId(prefix, serial, key) {
  return "sensor." + prefix + "ge_" + String(serial).toLowerCase() + "_" + key;
}

function entitiesFor(deviceId, serial, keys, prefix, omit, disabled) {
  return keys
    .filter(function (key) {
      return !(omit && omit.indexOf(key) !== -1);
    })
    .map(function (key) {
    return {
      entity_id: entityId(prefix, serial, key),
      platform: "givenergy_local",
      device_id: deviceId,
      unique_id: serial + "_" + key,
      area_id: prefix ? "loft" : null,
      disabled_by: disabled && disabled.indexOf(key) !== -1 ? "user" : null,
    };
  });
}

// opts: { inverterSerial, batterySerials[], ems, acCoupled, smartLoad, areaPrefix,
//         extraInverterSerial }
function makeHass(opts) {
  opts = opts || {};
  const prefix = opts.areaPrefix || "";
  const invSerial = opts.inverterSerial || "INV123";
  const bats = opts.batterySerials || [];
  const ems = !!opts.ems;

  const devices = [];
  const entities = [];

  if (ems) {
    devices.push({
      id: "dev_ems",
      identifiers: [["givenergy_local", invSerial]],
      name: "GivEnergy EMS " + invSerial,
      via_device_id: null,
    });
    entities.push.apply(entities, entitiesFor("dev_ems", invSerial, emsKeys(), prefix, opts.omitKeys));
  } else {
    devices.push({
      id: "dev_inv",
      identifiers: [["givenergy_local", invSerial]],
      name: "GivEnergy Inverter " + invSerial,
      via_device_id: null,
    });
    let invKeys = INVERTER_KEYS.slice();
    if (opts.smartLoad !== false) invKeys = invKeys.concat(SMART_LOAD_KEYS);
    if (opts.acCoupled) invKeys = invKeys.concat(AC_COUPLED_KEYS);
    entities.push.apply(
      entities,
      entitiesFor("dev_inv", invSerial, invKeys, prefix, opts.omitKeys, opts.disabledKeys)
    );

    bats.forEach(function (serial, i) {
      const id = "dev_bat" + (i + 1);
      devices.push({
        id: id,
        identifiers: [["givenergy_local", serial]],
        name: "GivEnergy Battery " + serial,
        via_device_id: "dev_inv",
      });
      entities.push.apply(
        entities,
        entitiesFor(id, serial, BATTERY_KEYS, prefix, opts.omitKeys, opts.disabledKeys)
      );
    });
  }

  // a second inverter plant, to exercise the serial pin / sole-plant selection
  if (opts.extraInverterSerial) {
    devices.push({
      id: "dev_inv2",
      identifiers: [["givenergy_local", opts.extraInverterSerial]],
      name: "GivEnergy Inverter " + opts.extraInverterSerial,
      via_device_id: null,
    });
    entities.push.apply(
      entities,
      entitiesFor("dev_inv2", opts.extraInverterSerial, INVERTER_KEYS, prefix, opts.omitKeys, opts.disabledKeys)
    );

    // batteries belonging to the SECOND plant (via_device -> dev_inv2), to prove
    // the first plant never borrows them.
    (opts.extraBatterySerials || []).forEach(function (serial, i) {
      const id = "dev_inv2_bat" + (i + 1);
      devices.push({
        id: id,
        identifiers: [["givenergy_local", serial]],
        name: "GivEnergy Battery " + serial,
        via_device_id: "dev_inv2",
      });
      entities.push.apply(
        entities,
        entitiesFor(id, serial, BATTERY_KEYS, prefix, opts.omitKeys, opts.disabledKeys)
      );
    });
  }

  // a foreign-integration entity that must be ignored
  entities.push({
    entity_id: "sensor.kitchen_temperature",
    platform: "other_integration",
    device_id: "dev_other",
    unique_id: "OTHER_temp",
    area_id: null,
  });

  return {
    callWS: function (msg) {
      if (msg.type === "config/entity_registry/list") return Promise.resolve(entities);
      if (msg.type === "config/device_registry/list") return Promise.resolve(devices);
      return Promise.reject(new Error("unexpected callWS: " + msg.type));
    },
  };
}

// haveCard() reads the global customElements; install/remove a stub that reports
// the given element names as registered.
function withCards(names, fn) {
  const reg = new Set(names);
  global.customElements = { get: (n) => (reg.has(n) ? class {} : undefined) };
  try {
    return fn();
  } finally {
    delete global.customElements;
  }
}

module.exports = {
  makeHass,
  withCards,
  entityId,
  INVERTER_KEYS,
  BATTERY_KEYS,
  SMART_LOAD_KEYS,
  AC_COUPLED_KEYS,
};
