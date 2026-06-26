// GivEnergy dashboard strategy (bundled with the givenergy_local integration
// and auto-registered as a frontend module - no manual install).
//
// Registers a Lovelace *dashboard strategy* `custom:givenergy` that generates
// the dashboard from the live registry on every render, so it never goes stale.
// v1 ships `mode: classic` - a faithful reproduction of the six-tab static
// dashboard layout, resolved from the live registry instead of frozen entity-id
// strings.
//
//   strategy:
//     type: custom:givenergy
//     mode: classic        # only mode in v1; unknown/absent -> classic
//     max_power_kw: 10     # Overview 24h chart y-axis envelope (+/- kW)
//     serial: SA2114G047   # optional inverter pin; default = sole/first plant
//
// Resolution rule (the fix for the dangling-ids rot): every entity is found in
// the entity registry by its stable `unique_id` (`{serial}_{key}`), then its
// *current* `entity_id` is read back. unique_id never changes on rename or area
// reassignment, so the `loft_` area-prefix bug cannot recur. We never construct
// or parse an entity_id string.
//
// NOTE: ASCII-only source on purpose - the /givenergy_local/ static serving path
// mangles multibyte UTF-8 (same lesson as ge-cell-heatmap.js), so card titles use
// "-"/"deg" rather than em-dash / degree-sign.

(function () {
  "use strict";

  // Register the strategy element immediately -- before any var assignments --
  // so customElements.whenDefined() resolves the instant this script is
  // evaluated, beating HA's timeout regardless of whether the module was served
  // from cache or freshly fetched. Function declarations (generateDashboard,
  // etc.) are hoisted in JS, so generate() can safely call generateDashboard
  // even though its textual definition appears later in the file.
  if (typeof customElements !== "undefined" &&
      !customElements.get("ll-strategy-dashboard-givenergy")) {
    customElements.define(
      "ll-strategy-dashboard-givenergy",
      class GivEnergyDashboardStrategy extends HTMLElement {
        static async generate(config, hass) {
          return generateDashboard(config, hass);
        }
      }
    );
  }

  var DOMAIN = "givenergy_local";

  // Battery Health palettes (mirrors dashboard.py _PACK_COLOURS / _TEMP_COLOURS).
  var PACK_COLOURS = ["#1e88e5", "#fb8c00", "#43a047", "#6d4c41", "#3949ab", "#c0ca33"];
  var TEMP_COLOURS = ["#00897b", "#8e24aa", "#d81b60", "#00838f", "#7cb342", "#5e35b1"];

  // Implausible single-sample rejection (mirrors dashboard.py). parseFloat (not
  // Number) so blank/unknown -> NaN -> gap rather than a spurious 0.
  var VOLT_FILTER = "const v = parseFloat(x); return (!isNaN(v) && v > 2.0 && v < 4.0) ? v : null;";
  var TEMP_FILTER = "const v = parseFloat(x); return (!isNaN(v) && v > -40 && v < 100) ? v : null;";
  var POWER_FILTER = "const v = parseFloat(x); return (!isNaN(v) && v > -20000 && v < 20000) ? v : null;";

  // The BMS samples one thermistor per 4-cell group; key suffix per group.
  var TEMP_GROUPS = [
    { key: "t_cells_01_04", lo: 1, hi: 4 },
    { key: "t_cells_05_08", lo: 5, hi: 8 },
    { key: "t_cells_09_12", lo: 9, hi: 12 },
    { key: "t_cells_13_16", lo: 13, hi: 16 },
  ];

  // ----- registry resolution -------------------------------------------------

  function classify(keys) {
    // By entity key, not device name - robust against user device renames.
    if (keys.has("v_cell_01") || keys.has("num_cycles") || keys.has("soc")) return "battery";
    if (keys.has("ems_plant_enable")) return "ems";
    if (keys.has("p_pv")) return "inverter";
    return "other";
  }

  // Resolve the plant topology and a key->entity_id map per device from the
  // registry. Returns { plants: [...], byDevice: Map<deviceId, Map<key, eid>> }.
  async function buildPlant(hass, opts) {
    var res;
    try {
      res = await Promise.all([
        hass.callWS({ type: "config/entity_registry/list" }),
        hass.callWS({ type: "config/device_registry/list" }),
      ]);
    } catch (err) {
      // These list commands aren't admin-gated, but the connection can still
      // fail (transient drop, reconnect in progress). Surface a friendly
      // notice rather than letting the whole strategy render throw.
      return { target: null, batteries: [], registryError: true };
    }
    var entities = res[0] || [];
    var devices = res[1] || [];

    // givenergy devices: deviceId -> { serial, viaDeviceId }
    var geDevices = new Map();
    for (var i = 0; i < devices.length; i++) {
      var d = devices[i];
      var ident = (d.identifiers || []).find(function (pair) {
        return pair[0] === DOMAIN;
      });
      if (!ident) continue;
      geDevices.set(d.id, { serial: ident[1], viaDeviceId: d.via_device_id || null });
    }

    // Two per-device maps, both keyed by stripping the `{serial}_` prefix off
    // each entity's unique_id. `allKeys` records every registered key incl.
    // disabled ones, used only for classification so disabling a single marker
    // entity (p_pv, ems_plant_enable, ...) can't make a whole device vanish.
    // `byDevice` holds only enabled entities' key->entity_id; disabled entities
    // have no state and would dangle if rendered.
    var byDevice = new Map();
    var allKeys = new Map();
    for (var j = 0; j < entities.length; j++) {
      var e = entities[j];
      if (e.platform !== DOMAIN) continue;
      var dev = geDevices.get(e.device_id);
      if (!dev || !e.unique_id) continue;
      var prefix = dev.serial + "_";
      if (e.unique_id.lastIndexOf(prefix, 0) !== 0) continue; // startsWith
      var key = e.unique_id.slice(prefix.length);
      var ks = allKeys.get(e.device_id);
      if (!ks) {
        ks = new Set();
        allKeys.set(e.device_id, ks);
      }
      ks.add(key);
      if (e.disabled_by) continue; // keep disabled entities out of the renderable map
      var m = byDevice.get(e.device_id);
      if (!m) {
        m = new Map();
        byDevice.set(e.device_id, m);
      }
      if (!m.has(key)) m.set(key, e.entity_id);
    }

    // classify each device against its FULL key set; collect inverters/ems + batteries
    var inverters = [];
    var batteries = [];
    geDevices.forEach(function (dev, deviceId) {
      var kind = classify(allKeys.get(deviceId) || new Set());
      var rec = {
        deviceId: deviceId,
        serial: dev.serial,
        viaDeviceId: dev.viaDeviceId,
        keys: byDevice.get(deviceId) || new Map(),
      };
      if (kind === "battery") batteries.push(rec);
      else if (kind === "inverter" || kind === "ems") {
        rec.isEms = kind === "ems";
        inverters.push(rec);
      }
    });

    inverters.sort(function (a, b) {
      return a.serial < b.serial ? -1 : a.serial > b.serial ? 1 : 0;
    });
    batteries.sort(function (a, b) {
      return a.serial < b.serial ? -1 : a.serial > b.serial ? 1 : 0;
    });

    // pick the target plant. An explicit serial pin that doesn't match must NOT
    // silently fall back to another plant - that would mis-target the
    // Maintenance buttons - so leave target null and let the no-plant notice
    // fire (naming the missing serial). The first-inverter default only applies
    // when no serial was supplied.
    var target = null;
    var unmatchedSerial = null;
    if (opts.serial) {
      target =
        inverters.find(function (p) {
          return String(p.serial).toUpperCase() === String(opts.serial).toUpperCase();
        }) || null;
      if (!target) unmatchedSerial = opts.serial;
    } else {
      // On an EMS plant prefer the controller (it carries the plant-level
      // aggregates and the real PV/grid/battery telemetry); a plain multi-inverter
      // plant still takes the first inverter by serial.
      var emsTarget = inverters.find(function (p) {
        return p.isEms;
      });
      target = emsTarget || inverters[0] || null;
    }

    // batteries belonging to the target inverter (by via_device). Only fall
    // back to all batteries when the registry exposes no via_device links at
    // all; when links exist, an empty match is genuine (this inverter has no
    // batteries) and must stay empty so we don't show another plant's packs.
    var anyViaLinks = batteries.some(function (b) {
      return b.viaDeviceId;
    });
    var ownBatteries = batteries.filter(function (b) {
      return target && b.viaDeviceId === target.deviceId;
    });
    if (!ownBatteries.length && !anyViaLinks) ownBatteries = batteries;

    return { target: target, batteries: ownBatteries, unmatchedSerial: unmatchedSerial };
  }

  // ----- small helpers -------------------------------------------------------

  function haveCard(name) {
    try {
      return typeof customElements !== "undefined" && !!customElements.get(name);
    } catch (e) {
      return false;
    }
  }

  function pad2(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  // entities-card row list: drop rows whose entity didn't resolve, then tidy
  // dividers (no leading/trailing/double dividers left dangling).
  function cleanRows(rows) {
    var kept = rows.filter(function (r) {
      return r && (r.type === "divider" || r.type === "button" || r.entity);
    });
    var out = [];
    for (var i = 0; i < kept.length; i++) {
      var r = kept[i];
      if (r.type === "divider") {
        var prev = out[out.length - 1];
        if (!prev || prev.type === "divider") continue; // skip leading/double
      }
      out.push(r);
    }
    while (out.length && out[out.length - 1].type === "divider") out.pop();
    return out;
  }

  function placeholder(cardName) {
    return {
      type: "markdown",
      content:
        "**" +
        cardName +
        "** is not installed. Install it via **HACS > Frontend**, then reload this dashboard.",
    };
  }

  // ----- classic mode --------------------------------------------------------

  // `inv(key)` / `bat(rec, key)` return the resolved entity_id or null.
  function makeAccessors(plant) {
    var invKeys = (plant.target && plant.target.keys) || new Map();
    return {
      inv: function (key) {
        return invKeys.get(key) || null;
      },
      // Plant load: prefer the EMS aggregate (the controller's own p_load_demand is
      // gated off on EMS), falling back to the inverter key on a plain plant.
      load: function () {
        return invKeys.get("ems_calc_load_power") || invKeys.get("p_load_demand") || null;
      },
      bat: function (rec, key) {
        return rec.keys.get(key) || null;
      },
    };
  }

  function row(entity, name) {
    return entity ? { entity: entity, name: name } : { entity: null };
  }

  function classicViews(plant, opts) {
    var a = makeAccessors(plant);
    var views = [];
    if (plant.target && plant.target.isEms) {
      // The EMS controller carries real plant telemetry (#206), so it gets the
      // Overview + Energy views. Batteries hang off the managed inverters and the
      // inverter-level controls are suppressed on EMS, so those views are replaced
      // by the EMS controls (slots) + diagnostics.
      views.push(overviewView(plant, a, opts));
      views.push(energyView(plant, a));
      views.push(emsControlsView(plant, a));
      views.push(emsDiagnosticsView(plant, a));
      return views;
    }
    views.push(overviewView(plant, a, opts));
    views.push(energyView(plant, a));
    views.push(batteriesView(plant, a));
    if (plant.batteries.length) views.push(batteryHealthView(plant, a));
    views.push(controlsView(plant, a));
    views.push(diagnosticsView(plant, a, opts));
    return views;
  }

  // ----- flow mode -----------------------------------------------------------

  // The Flow panel prepended to the full classic view set. Nothing is dropped:
  // the classic views stay available; per-mode pruning is a later decision.
  function flowViews(plant, opts) {
    var a = makeAccessors(plant);
    // The EMS controller carries real PV/grid/battery telemetry (#206), so the
    // flow panel renders there too; its load node uses the EMS load aggregate.
    return [flowView(plant, a, opts)].concat(classicViews(plant, opts));
  }

  function flowView(plant, a, opts) {
    var cfg = { type: "custom:givenergy-flow", max_power_kw: opts.maxPowerKw || 10 };
    if (a.inv("p_pv")) cfg.solar = a.inv("p_pv");
    var strings = [a.inv("p_pv1"), a.inv("p_pv2")].filter(Boolean);
    if (strings.length) cfg.solar_strings = strings;
    if (a.inv("grid_power")) cfg.grid = a.inv("grid_power");
    var load = a.load();
    if (load) cfg.load = load;
    if (a.inv("p_battery")) cfg.battery_power = a.inv("p_battery");
    if (a.inv("battery_soc")) cfg.battery_soc = a.inv("battery_soc");

    var packs = plant.batteries
      .map(function (b) {
        return { name: String(b.serial).toUpperCase(), soc: a.bat(b, "soc") };
      })
      .filter(function (p) {
        return p.soc;
      });
    if (packs.length) cfg.packs = packs;

    // Today-totals strip: card-slot name -> entity key (the same keys the
    // classic Overview "Today" glance uses).
    var totalKeys = {
      pv_today: "e_pv_day",
      charge_today: "e_battery_charge_day",
      discharge_today: "e_battery_discharge_day",
      import_today: "e_grid_in_day",
      export_today: "e_grid_out_day",
      house_today: "e_consumption_today",
    };
    var totals = {};
    Object.keys(totalKeys).forEach(function (slot) {
      var eid = a.inv(totalKeys[slot]);
      if (eid) totals[slot] = eid;
    });
    if (Object.keys(totals).length) cfg.totals = totals;

    var view = {
      title: "Flow",
      path: "flow",
      icon: "mdi:transit-connection-variant",
      panel: true,
      cards: [cfg],
    };
    // Kiosk-mode hints (feature-detected). Omit when the integration isn't
    // present so the dashboard still works inside the standard HA shell.
    if (haveCard("kiosk-mode")) {
      view.kiosk_mode = { hide_header: true, hide_sidebar: true };
    }
    return view;
  }

  function glanceViews(plant, opts) {
    var a = makeAccessors(plant);
    // The EMS controller carries real PV/grid/battery telemetry (#206), so the
    // glance panel renders there too; its load tile uses the EMS load aggregate.
    return [glanceView(plant, a, opts)].concat(classicViews(plant, opts));
  }

  function glanceView(plant, a, opts) {
    var cfg = { type: "custom:givenergy-glance" };
    if (a.inv("p_pv")) cfg.solar = a.inv("p_pv");
    var strings = [a.inv("p_pv1"), a.inv("p_pv2")].filter(Boolean);
    if (strings.length) cfg.solar_strings = strings;
    if (a.inv("grid_power")) cfg.grid = a.inv("grid_power");
    var load = a.load();
    if (load) cfg.load = load;
    if (a.inv("p_battery")) cfg.battery_power = a.inv("p_battery");
    if (a.inv("battery_soc")) cfg.battery_soc = a.inv("battery_soc");

    var packs = plant.batteries
      .map(function (b) {
        return { name: String(b.serial).toUpperCase(), soc: a.bat(b, "soc") };
      })
      .filter(function (p) { return p.soc; });
    if (packs.length) cfg.packs = packs;

    // Totals: subset of the flow card's slots (no charge/discharge here).
    var totalKeys = {
      pv_today: "e_pv_day",
      import_today: "e_grid_in_day",
      export_today: "e_grid_out_day",
      house_today: "e_consumption_today",
    };
    var totals = {};
    Object.keys(totalKeys).forEach(function (slot) {
      var eid = a.inv(totalKeys[slot]);
      if (eid) totals[slot] = eid;
    });
    if (Object.keys(totals).length) cfg.totals = totals;

    var view = {
      title: "Glance",
      path: "glance",
      icon: "mdi:eye-outline",
      panel: true,
      cards: [cfg],
    };
    if (haveCard("kiosk-mode")) {
      view.kiosk_mode = { hide_header: true, hide_sidebar: true };
    }
    return view;
  }

  // mode: all -- Glance + Flow + Analyst panels followed by the classic tab set.
  // Not intended as a permanent user-facing mode; remove when modes split into
  // separate dashboards.
  function allViews(plant, opts) {
    var a = makeAccessors(plant);
    // Glance + Flow render on EMS (controller telemetry, #206); Analyst is held
    // back there - its energy ledger needs daily battery/house figures the
    // controller doesn't surface (#52).
    if (plant.target && plant.target.isEms)
      return [glanceView(plant, a, opts), flowView(plant, a, opts)].concat(classicViews(plant, opts));
    return [glanceView(plant, a, opts), flowView(plant, a, opts), analystView(plant, a, opts)].concat(classicViews(plant, opts));
  }

  // mode: analyst -- dense terminal-aesthetic view for diagnostics / debugging /
  // optimisation. Non-panel multi-card view: givenergy-analyst card (live metrics +
  // energy ledger + diagnostics table), apexcharts 24h power overlay, and one
  // ge-cell-heatmap per battery pack. Analyst is inverter-centric; falls back
  // to classic for EMS plants.
  function analystViews(plant, opts) {
    var a = makeAccessors(plant);
    if (plant.target && plant.target.isEms) return classicViews(plant, opts);
    return [analystView(plant, a, opts)].concat(classicViews(plant, opts));
  }

  function analystView(plant, a, opts) {
    var cards = [];

    // Card 1: custom element handles live metrics, energy ledger, diagnostics.
    var cfg = { type: "custom:givenergy-analyst" };
    if (a.inv("p_pv"))          cfg.solar        = a.inv("p_pv");
    var strings = [a.inv("p_pv1"), a.inv("p_pv2")].filter(Boolean);
    if (strings.length)         cfg.solar_strings = strings;
    if (a.inv("grid_power"))    cfg.grid          = a.inv("grid_power");
    if (a.inv("p_load_demand")) cfg.load          = a.inv("p_load_demand");
    if (a.inv("p_battery"))     cfg.battery_power = a.inv("p_battery");
    if (a.inv("battery_soc"))   cfg.battery_soc   = a.inv("battery_soc");

    var totalKeyMap = {
      pv_today:        "e_pv_day",
      discharge_today: "e_battery_discharge_day",
      import_today:    "e_grid_in_day",
      house_today:     "e_consumption_today",
      charge_today:    "e_battery_charge_day",
      export_today:    "e_grid_out_day",
    };
    var totals = {};
    Object.keys(totalKeyMap).forEach(function (slot) {
      var eid = a.inv(totalKeyMap[slot]);
      if (eid) totals[slot] = eid;
    });
    if (Object.keys(totals).length) cfg.totals = totals;

    var diagKeyMap = {
      t_inverter_heatsink:  "t_inverter_heatsink",
      t_charger:            "t_charger",
      f_ac1:                "f_ac1",
      pf_inverter:          "pf_inverter_output_now",
      work_time_total:      "work_time_total",
      consecutive_failures: "consecutive_failures",
      last_refresh:         "last_successful_refresh",
    };
    var diag = {};
    Object.keys(diagKeyMap).forEach(function (slot) {
      var eid = a.inv(diagKeyMap[slot]);
      if (eid) diag[slot] = eid;
    });
    if (Object.keys(diag).length) cfg.diag = diag;

    cards.push(cfg);

    // Card 2: 24h power overlay.
    var powerSeries = [
      { entity: a.inv("p_pv"),          name: "PV",      color: "#d4a85a" },
      { entity: a.inv("p_load_demand"), name: "Load",    color: "#cfcfca" },
      { entity: a.inv("p_battery"),     name: "Battery", color: "#5bbb6a" },
      { entity: a.inv("grid_power"),    name: "Grid",    color: "#4a9fd4" },
    ].filter(function (s) { return s.entity; });

    if (haveCard("apexcharts-card") && powerSeries.length) {
      var cap = (opts.maxPowerKw || 10) * 1000;
      cards.push({
        type: "custom:apexcharts-card",
        header: { show: true, title: "Power - last 24 h" },
        graph_span: "24h",
        yaxis: [{ min: -cap, max: cap }],
        series: powerSeries,
      });
    } else if (powerSeries.length) {
      cards.push(placeholder("apexcharts-card"));
    }

    // Card 3+: ge-cell-heatmap per battery pack.
    plant.batteries.forEach(function (b) {
      cards.push({
        type: "custom:ge-cell-heatmap",
        title: "Cell balance - " + String(b.serial).toUpperCase(),
        batteries: [b.serial],
      });
    });

    return {
      title: "Analyst",
      path: "analyst",
      icon: "mdi:chart-line",
      cards: cards,
    };
  }

  function overviewView(plant, a, opts) {
    var cap = (opts.maxPowerKw || 10) * 1000;
    var cards = [];

    if (haveCard("power-flow-card-plus")) {
      var ents = {};
      if (a.inv("p_pv")) ents.solar = { entity: a.inv("p_pv"), display_zero_state: true };
      if (a.inv("p_battery")) {
        ents.battery = { entity: a.inv("p_battery") };
        if (a.inv("battery_soc")) ents.battery.state_of_charge = a.inv("battery_soc");
      }
      // grid_power is signed +ve=export (GE convention); power-flow-card-plus
      // expects +ve=import, so invert or the grid bubble shows import as export (#212).
      if (a.inv("grid_power")) ents.grid = { entity: a.inv("grid_power"), invert_state: true };
      if (a.load()) ents.home = { entity: a.load() };
      cards.push({ type: "custom:power-flow-card-plus", entities: ents });
    } else {
      cards.push(placeholder("power-flow-card-plus"));
    }

    if (plant.target && plant.target.isEms) {
      // The controller's distinguishing plant-level aggregates (the inverter-keyed
      // cards above show the real PV/grid/battery; these complement them). Measured
      // load is omitted - it reads zero on current firmware.
      cards.push({
        type: "entities",
        title: "EMS Plant",
        entities: cleanRows([
          row(a.inv("ems_calc_load_power"), "Calculated Load"),
          row(a.inv("ems_grid_meter_power"), "Grid Meter Power"),
          row(a.inv("ems_total_battery_power"), "Total Battery Power"),
          row(a.inv("ems_remaining_battery_energy"), "Remaining Battery Energy"),
          row(a.inv("ems_inverter_count"), "Managed Inverters"),
        ]),
      });
    }

    cards.push({
      type: "glance",
      title: "Status",
      columns: 4,
      entities: cleanRows([
        row(a.inv("status"), "Inverter"),
        row(a.inv("battery_soc"), "Battery SOC"),
        row(a.inv("battery_pause_mode"), "Pause Mode"),
        row(a.inv("t_battery"), "Battery Temp"),
        row(a.inv("battery_out_of_spec"), "Battery OOS"),
      ]),
    });

    cards.push({
      type: "glance",
      title: "Today",
      columns: 6,
      entities: cleanRows([
        row(a.inv("e_pv_day"), "PV"),
        row(a.inv("e_battery_charge_day"), "Charged"),
        row(a.inv("e_battery_discharge_day"), "Discharged"),
        row(a.inv("e_grid_in_day"), "Imported"),
        row(a.inv("e_grid_out_day"), "Exported"),
        row(a.inv("e_consumption_today"), "Consumed"),
      ]),
    });

    var series = [
      { entity: a.inv("p_pv"), name: "PV", color: "#FFB300" },
      { entity: a.inv("p_battery"), name: "Battery", color: "#42A5F5" },
      { entity: a.inv("grid_power"), name: "Grid", color: "#66BB6A" },
      { entity: a.load(), name: "Load", color: "#EF5350" },
    ].filter(function (s) {
      return s.entity;
    });
    if (haveCard("apexcharts-card")) {
      cards.push({
        type: "custom:apexcharts-card",
        header: { show: true, title: "Power - Last 24 Hours" },
        graph_span: "24h",
        yaxis: [{ min: -cap, max: cap }],
        series: series,
      });
    } else {
      cards.push(placeholder("apexcharts-card"));
    }

    return { title: "Overview", path: "overview", icon: "mdi:solar-power-variant", cards: cards };
  }

  function colSeries(entity, name, color) {
    return {
      entity: entity,
      name: name,
      color: color,
      type: "column",
      statistics: { type: "state", period: "hour" },
      group_by: { func: "max", duration: "1d" },
    };
  }

  function energyView(plant, a) {
    var cards = [];
    function apexPair(title, s1, s2) {
      var series = [s1, s2].filter(function (s) {
        return s.entity;
      });
      if (!series.length) return null;
      if (!haveCard("apexcharts-card")) return placeholder("apexcharts-card");
      return {
        type: "custom:apexcharts-card",
        header: { show: true, title: title },
        graph_span: "30d",
        series: series,
      };
    }

    [
      apexPair(
        "Daily Generation vs Consumption - Last 30 Days",
        colSeries(a.inv("e_pv_day"), "PV Generated", "#FFB300"),
        colSeries(a.inv("e_consumption_today"), "Consumed", "#EF5350")
      ),
      apexPair(
        "Grid Import vs Export - Last 30 Days",
        colSeries(a.inv("e_grid_out_day"), "Exported", "#66BB6A"),
        colSeries(a.inv("e_grid_in_day"), "Imported", "#EF5350")
      ),
      apexPair(
        "Battery Charge vs Discharge - Last 30 Days",
        colSeries(a.inv("e_battery_charge_day"), "Charged", "#42A5F5"),
        colSeries(a.inv("e_battery_discharge_day"), "Discharged", "#7E57C2")
      ),
    ].forEach(function (c) {
      if (c) cards.push(c);
    });

    cards.push({
      type: "entities",
      title: "All-Time Totals",
      entities: cleanRows([
        row(a.inv("e_pv_total"), "PV Generated"),
        row(a.inv("e_battery_throughput"), "Battery Throughput"),
        row(a.inv("e_battery_charge_total"), "Battery Charged"),
        row(a.inv("e_battery_discharge_total"), "Battery Discharged"),
        row(a.inv("e_grid_out_total"), "Grid Exported"),
        row(a.inv("e_grid_in_total"), "Grid Imported"),
        row(a.inv("e_pv_generation_total"), "PV Generation Total"),
        row(a.inv("e_inverter_in_total"), "Charged from Grid"),
        row(a.inv("e_discharge_year"), "Discharged This Year"),
        row(a.inv("e_solar_diverter"), "Solar Diverter Energy"),
      ]),
    });

    return { title: "Energy", path: "energy", icon: "mdi:lightning-bolt", cards: cards };
  }

  function batteriesView(plant, a) {
    var sections = plant.batteries.map(function (rec) {
      var cards = [];
      if (a.bat(rec, "soc")) {
        cards.push({
          type: "gauge",
          entity: a.bat(rec, "soc"),
          name: String(rec.serial).toUpperCase(),
          min: 0,
          max: 100,
          needle: true,
          severity: { red: 0, yellow: 20, green: 40 },
        });
      }
      cards.push({
        type: "entities",
        title: "Pack Details",
        entities: cleanRows([
          row(a.bat(rec, "soc"), "SOC"),
          row(a.bat(rec, "v_out"), "Voltage"),
          row(a.bat(rec, "t_max"), "Temp Max"),
          row(a.bat(rec, "t_min"), "Temp Min"),
          row(a.bat(rec, "t_bms_mosfet"), "BMS MOSFET Temp"),
          { type: "divider" },
          row(a.bat(rec, "num_cycles"), "Charge Cycles"),
          row(a.bat(rec, "cap_remaining"), "Remaining Capacity"),
          row(a.bat(rec, "cap_calibrated"), "Calibrated Capacity"),
          row(a.bat(rec, "cap_design"), "Design Capacity"),
          row(a.bat(rec, "v_cells_sum"), "Cell Voltages Sum"),
          row(a.bat(rec, "num_cells"), "Cell Count"),
        ]),
      });
      cards.push({
        type: "entities",
        title: "Cell Temperatures",
        entities: cleanRows([
          row(a.bat(rec, "t_cells_01_04"), "Cells 1-4"),
          row(a.bat(rec, "t_cells_05_08"), "Cells 5-8"),
          row(a.bat(rec, "t_cells_09_12"), "Cells 9-12"),
          row(a.bat(rec, "t_cells_13_16"), "Cells 13-16"),
        ]),
      });
      var bms = [
        row(a.bat(rec, "bms_firmware_version"), "BMS Firmware"),
        row(a.bat(rec, "usb_device_inserted"), "USB Device"),
        row(a.bat(rec, "cap_design2"), "Design Capacity Alt"),
        { type: "divider" },
      ];
      for (var s = 1; s <= 7; s++) bms.push(row(a.bat(rec, "status_" + s), "Status " + s));
      bms.push({ type: "divider" });
      bms.push(row(a.bat(rec, "warning_1"), "Warning 1"));
      bms.push(row(a.bat(rec, "warning_2"), "Warning 2"));
      cards.push({ type: "entities", title: "BMS Diagnostics", entities: cleanRows(bms) });

      return { type: "grid", cards: cards };
    });

    return {
      title: "Batteries",
      path: "battery",
      type: "sections",
      icon: "mdi:battery-high",
      sections: sections,
    };
  }

  function healthAnnotations() {
    var amber = "#f9a825";
    function warnLine(y, text) {
      return {
        y: y,
        borderColor: amber,
        strokeDashArray: 0,
        label: {
          text: text,
          position: "left",
          textAnchor: "start",
          borderColor: amber,
          style: { color: "#000", background: amber },
        },
      };
    }
    return [
      { y: 3.5, y2: 3.6, fillColor: amber, opacity: 0.14 },
      { y: 2.9, y2: 3.0, fillColor: amber, opacity: 0.14 },
      warnLine(3.5, "warn - 3.50 V / 45 degC"),
      warnLine(3.0, "warn - 3.00 V / 10 degC"),
    ];
  }

  function batteryHealthView(plant, a) {
    var voltSeries = [];
    var tempSeries = [];
    var socSeries = [];
    plant.batteries.forEach(function (rec, index) {
      var tag = "B" + (index + 1);
      var packColour = PACK_COLOURS[index % PACK_COLOURS.length];
      var tempColour = TEMP_COLOURS[index % TEMP_COLOURS.length];
      for (var cell = 1; cell <= 16; cell++) {
        var ve = a.bat(rec, "v_cell_" + pad2(cell));
        if (!ve) continue;
        voltSeries.push({
          entity: ve,
          name: tag + " " + cell,
          color: packColour,
          stroke_width: 1,
          yaxis_id: "v",
          transform: VOLT_FILTER,
        });
      }
      TEMP_GROUPS.forEach(function (g) {
        var tempEid = a.bat(rec, g.key);
        if (!tempEid) return;
        tempSeries.push({
          entity: tempEid,
          name: tag + " T" + g.lo + "-" + g.hi,
          color: tempColour,
          stroke_width: 1,
          yaxis_id: "temp",
          transform: TEMP_FILTER,
        });
      });
      var se = a.bat(rec, "soc");
      if (se) {
        socSeries.push({
          entity: se,
          name: tag + " SoC",
          color: packColour,
          stroke_width: 1,
          yaxis_id: "soc",
        });
      }
    });

    var note = {
      type: "markdown",
      content:
        "## Battery health\n" +
        "Cross-pack cell diagnostics. **Heatmap**: each cell coloured by its mV " +
        "deviation from its own pack's mean (imbalance shows at any charge level). " +
        "**Cell voltages + temperatures**: every cell (left) with cell-group " +
        "temperatures (right) on a shared warn band. **Power + SoC**: the " +
        "charge/discharge rate driving each pack's state of charge. Implausible " +
        "single-sample reads are filtered to gaps.",
    };
    var heatmap = {
      type: "custom:ge-cell-heatmap",
      title: "Cell balance - deviation from pack mean",
      batteries: plant.batteries.map(function (rec) {
        return rec.serial;
      }),
    };
    var cards = [note, heatmap];

    if (voltSeries.length || tempSeries.length) {
      cards.push({
        type: "custom:apexcharts-card",
        header: {
          show: true,
          title:
            "Cell voltages (left, V) + cell-group temps (right, degC - warn bands shared) - 24h",
        },
        graph_span: "24h",
        chart_type: "line",
        yaxis: [
          { id: "v", min: 2.9, max: 3.6, decimals: 2 },
          { id: "temp", opposite: true, min: 3, max: 52, decimals: 0 },
        ],
        apex_config: {
          legend: { show: false },
          annotations: { yaxis: healthAnnotations() },
          chart: { height: 330 },
        },
        series: voltSeries.concat(tempSeries),
      });
    }

    var powerEntity = a.inv("p_battery");
    var powerSeries = [];
    if (powerEntity) {
      powerSeries.push({
        entity: powerEntity,
        name: "Battery power",
        color: "#8e24aa",
        stroke_width: 1,
        transform: POWER_FILTER,
        yaxis_id: "w",
        group_by: { duration: "2m", func: "avg" },
      });
    }
    powerSeries = powerSeries.concat(socSeries);
    if (powerSeries.length) {
      cards.push({
        type: "custom:apexcharts-card",
        header: { show: true, title: "Battery power (left, W, 2-min avg) + pack SoC (right) - 24h" },
        graph_span: "24h",
        chart_type: "line",
        apex_config: {
          legend: { show: false },
          annotations: {
            yaxis: [
              {
                y: 0,
                borderColor: "#616161",
                strokeDashArray: 3,
                label: {
                  text: "0 W (idle)",
                  position: "left",
                  textAnchor: "start",
                  style: { color: "#000", background: "#e0e0e0" },
                },
              },
            ],
          },
          chart: { height: 330 },
        },
        series: powerSeries,
        yaxis: [
          { id: "w", decimals: 0 },
          { id: "soc", opposite: true, min: 0, max: 100, decimals: 0 },
        ],
      });
    }

    // ApexCharts may be absent; swap the chart cards for a single placeholder.
    if (!haveCard("apexcharts-card")) {
      cards = [note, heatmap, placeholder("apexcharts-card")];
    }
    cards.forEach(function (c) {
      c.grid_options = { columns: "full" };
    });

    return {
      title: "Battery Health",
      path: "battery-health",
      type: "sections",
      icon: "mdi:heart-pulse",
      sections: [{ type: "grid", cards: cards }],
    };
  }

  function controlsView(plant, a) {
    var cards = [];

    cards.push({
      type: "entities",
      title: "Mode",
      entities: cleanRows([
        row(a.inv("battery_power_mode"), "Battery Power Mode"),
        row(a.inv("battery_pause_mode"), "Pause Mode"),
        row(a.inv("enable_rtc"), "Real Time Control"),
        row(a.inv("active_power_rate"), "Inverter Max Output Power"),
        row(a.inv("battery_calibration_stage"), "Calibration Stage"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Charging",
      entities: cleanRows([
        row(a.inv("enable_charge"), "Enable Charge"),
        row(a.inv("charge_target_soc"), "Charge Target SOC"),
        row(a.inv("battery_soc_reserve"), "SOC Reserve"),
        row(a.inv("battery_charge_limit"), "Charge Power Limit"),
        { type: "divider" },
        row(a.inv("charge_slot_1_start"), "Slot 1 Start"),
        row(a.inv("charge_slot_1_end"), "Slot 1 End"),
        row(a.inv("charge_slot_2_start"), "Slot 2 Start"),
        row(a.inv("charge_slot_2_end"), "Slot 2 End"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Discharging",
      entities: cleanRows([
        row(a.inv("enable_discharge"), "Enable Discharge"),
        row(a.inv("battery_discharge_limit"), "Discharge Power Limit"),
        row(a.inv("battery_discharge_min_power_reserve"), "Min Power Reserve"),
        { type: "divider" },
        row(a.inv("discharge_slot_1_start"), "Slot 1 Start"),
        row(a.inv("discharge_slot_1_end"), "Slot 1 End"),
        row(a.inv("discharge_slot_2_start"), "Slot 2 Start"),
        row(a.inv("discharge_slot_2_end"), "Slot 2 End"),
      ]),
    });

    // AC-coupled controls exist only when the plant carries the HR(300-359) block
    // (the integration creates these entities conditionally) - feature-detect.
    if (a.inv("export_priority")) {
      cards.push({
        type: "entities",
        title: "AC-Coupled",
        entities: cleanRows([
          row(a.inv("export_priority"), "Export Priority"),
          row(a.inv("enable_eps"), "EPS Enable"),
          row(a.inv("battery_charge_limit_ac"), "AC Charge Limit"),
          row(a.inv("battery_discharge_limit_ac"), "AC Discharge Limit"),
        ]),
      });
    }

    // Smart Load slots exist on non-EMS inverters - feature-detect on slot 1.
    if (a.inv("smart_load_slot_1_start")) {
      var sl = [];
      for (var idx = 1; idx <= 10; idx++) {
        if (idx > 1) sl.push({ type: "divider" });
        sl.push(row(a.inv("smart_load_slot_" + idx + "_start"), "Slot " + idx + " Start"));
        sl.push(row(a.inv("smart_load_slot_" + idx + "_end"), "Slot " + idx + " End"));
      }
      cards.push({ type: "entities", title: "Smart Load", entities: cleanRows(sl) });
    }

    var serial = String(plant.target.serial).toUpperCase();
    cards.push({
      type: "entities",
      title: "Maintenance",
      entities: [
        {
          type: "button",
          name: "Redetect Plant",
          icon: "mdi:radar",
          tap_action: {
            action: "perform-action",
            perform_action: "givenergy_local.redetect_plant",
            confirmation: {
              text:
                "This will reload the GivEnergy integration and force a full " +
                "hardware detection sweep. Continue?",
            },
            data: { serial: serial },
          },
        },
        {
          type: "button",
          name: "Sync Inverter Clock",
          icon: "mdi:clock-sync-outline",
          tap_action: {
            action: "perform-action",
            perform_action: "givenergy_local.set_system_datetime",
            data: { serial: serial },
          },
        },
      ],
    });

    return { title: "Controls", path: "controls", icon: "mdi:tune", cards: cards };
  }

  function integrationHealthCard(a) {
    return {
      type: "entities",
      title: "Integration Health",
      entities: cleanRows([
        row(a.inv("last_successful_refresh"), "Last Successful Refresh"),
        row(a.inv("consecutive_failures"), "Consecutive Failures"),
        row(a.inv("partial_failures"), "Partial Failures"),
        row(a.inv("total_failures"), "Total Failures"),
      ]),
    };
  }

  function diagnosticsView(plant, a, opts) {
    var cards = [];
    cards.push(integrationHealthCard(a));

    cards.push({
      type: "entities",
      title: "Faults & Warnings",
      entities: cleanRows([
        row(a.inv("status"), "Inverter Status"),
        row(a.inv("battery_out_of_spec"), "Battery Out Of Spec"),
        row(a.inv("fault_code"), "Fault Code"),
        row(a.inv("inverter_fault_messages"), "Fault Messages"),
        row(a.inv("inverter_errors"), "Inverter Errors"),
        row(a.inv("charger_warning_code"), "Charger Warning Code"),
        row(a.inv("charge_status"), "Charge Status (raw)"),
        row(a.inv("system_mode"), "System Mode (raw)"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Temperatures",
      entities: cleanRows([
        row(a.inv("t_battery"), "Battery"),
        row(a.inv("t_inverter_heatsink"), "Inverter Heatsink"),
        row(a.inv("t_charger"), "Charger"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Electrical",
      entities: cleanRows([
        row(a.inv("v_ac1"), "AC Voltage (input)"),
        row(a.inv("f_ac1"), "AC Frequency (input)"),
        row(a.inv("v_ac1_output"), "AC Voltage (output)"),
        row(a.inv("f_ac1_output"), "AC Frequency (output)"),
        row(a.inv("i_ac1"), "AC Current (output)"),
        row(a.inv("v_battery"), "Battery Voltage"),
        row(a.inv("i_battery"), "Battery Current"),
        row(a.inv("i_grid_port"), "Grid Port Current"),
        row(a.inv("v_p_bus"), "Positive DC Bus"),
        row(a.inv("v_n_bus"), "Negative DC Bus"),
        row(a.inv("p_grid_apparent"), "Grid Apparent Power"),
        row(a.inv("pf_inverter_output_now"), "Inverter Power Factor"),
        row(a.inv("p_grid_out_ph1"), "Grid Power Phase 1"),
        row(a.inv("p_backup"), "Backup Power"),
        row(a.inv("p_combined_generation"), "Combined Generation Power"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "PV Strings",
      entities: cleanRows([
        row(a.inv("v_pv1"), "String 1 Voltage"),
        row(a.inv("i_pv1"), "String 1 Current"),
        row(a.inv("p_pv1"), "String 1 Power"),
        row(a.inv("v_pv2"), "String 2 Voltage"),
        row(a.inv("i_pv2"), "String 2 Current"),
        row(a.inv("p_pv2"), "String 2 Power"),
        { type: "divider" },
        row(a.inv("e_pv1_day"), "String 1 Energy Today"),
        row(a.inv("e_pv2_day"), "String 2 Energy Today"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Hardware & Firmware",
      entities: cleanRows([
        row(a.inv("battery_maintenance_mode"), "Battery Maintenance Mode"),
        row(a.inv("arm_firmware_version"), "ARM Firmware"),
        row(a.inv("dsp_firmware_version"), "DSP Firmware"),
        row(a.inv("modbus_version"), "Modbus Version"),
        row(a.inv("work_time_total"), "Work Time"),
        row(a.inv("device_type_code"), "Device Type Code"),
        row(a.inv("num_mppt"), "MPPT Count"),
        row(a.inv("num_phases"), "Phase Count"),
        row(a.inv("battery_type"), "Battery Type"),
        row(a.inv("meter_type"), "Meter Type"),
        row(a.inv("usb_device_inserted"), "USB Device"),
        row(a.inv("battery_capacity_kwh"), "Nominal Capacity (kWh)"),
        row(a.inv("battery_capacity_ah"), "Capacity (Ah)"),
      ]),
    });

    cards.push({
      type: "entities",
      title: "Integration",
      entities: [
        {
          type: "button",
          name: "Capture Debug Frames (60 s)",
          icon: "mdi:bug-outline",
          action_name: "Run",
          tap_action: {
            action: "perform-action",
            perform_action: "givenergy_local.capture_frames",
            data: { duration: 60 },
          },
        },
      ],
    });

    return { title: "Diagnostics", path: "diagnostics", icon: "mdi:wrench", cards: cards };
  }

  // ----- EMS plant views -----------------------------------------------------

  function emsSlotCard(a, kind, title) {
    var rows = [];
    for (var idx = 1; idx <= 3; idx++) {
      if (idx > 1) rows.push({ type: "divider" });
      rows.push(row(a.inv("ems_" + kind + "_slot_" + idx + "_start"), "Slot " + idx + " Start"));
      rows.push(row(a.inv("ems_" + kind + "_slot_" + idx + "_end"), "Slot " + idx + " End"));
      rows.push(
        row(a.inv("ems_" + kind + "_target_soc_" + idx), "Slot " + idx + " Target SOC")
      );
    }
    return { type: "entities", title: title, entities: cleanRows(rows) };
  }

  function emsControlsView(plant, a) {
    var cards = [
      {
        type: "entities",
        title: "Plant",
        entities: cleanRows([row(a.inv("ems_plant_enable"), "Flexi EMS Control")]),
      },
      emsSlotCard(a, "charge", "Charge Slots"),
      emsSlotCard(a, "discharge", "Discharge Slots"),
      emsSlotCard(a, "export", "Export Slots"),
    ];
    return { title: "EMS Controls", path: "ems-controls", icon: "mdi:tune", cards: cards };
  }

  function emsDiagnosticsView(plant, a) {
    return {
      title: "Diagnostics",
      path: "diagnostics",
      icon: "mdi:wrench",
      cards: [integrationHealthCard(a)],
    };
  }

  // ----- entry point ---------------------------------------------------------

  async function generateDashboard(config, hass) {
    config = config || {};
    var opts = {
      maxPowerKw: config.max_power_kw != null ? config.max_power_kw : 10,
      serial: config.serial || null,
      mode: config.mode || "classic",
    };
    var plant = await buildPlant(hass, opts);
    if (!plant.target) {
      var notice;
      if (plant.registryError) {
        notice =
          "Could not read the entity registry from Home Assistant - usually a " +
          "transient connection issue. Try reloading the dashboard.";
      } else if (plant.unmatchedSerial) {
        notice =
          "No GivEnergy inverter matches the pinned serial **" +
          String(plant.unmatchedSerial).toUpperCase() +
          "**. Check the `serial:` in this dashboard's strategy config.";
      } else {
        notice =
          "No GivEnergy plant found in the registry. Is the **givenergy_local** " +
          "integration set up and connected?";
      }
      return {
        title: "GivEnergy",
        views: [
          {
            title: "GivEnergy",
            cards: [{ type: "markdown", content: notice }],
          },
        ],
      };
    }
    // Mode dispatch. Unknown/absent mode falls back to classic.
    var views =
      opts.mode === "flow"    ? flowViews(plant, opts)    :
      opts.mode === "glance"  ? glanceViews(plant, opts)  :
      opts.mode === "analyst" ? analystViews(plant, opts) :
      opts.mode === "all"     ? allViews(plant, opts)     :
                                classicViews(plant, opts);
    return { title: "GivEnergy", views: views };
  }

  var API = {
    buildPlant: buildPlant,
    classicViews: classicViews,
    generateDashboard: generateDashboard,
  };

  // Browser: register remaining custom elements. The strategy element
  // (ll-strategy-dashboard-givenergy) is already registered at the top of this
  // IIFE. The heatmap card is defined here (inside the guard) so that Node
  // (vitest) never evaluates `extends HTMLElement`, which doesn't exist there.
  if (typeof customElements !== "undefined") {
    // custom:ge-cell-heatmap - merged from ge-cell-heatmap.js.
    // Renders one row per battery pack: each of the 16 cell voltages coloured
    // by its mV deviation from that pack's own mean (imbalance visible at any
    // charge level), plus the pack mean (V) and spread (max-min, mV).
    // Config: type / batteries (required) / cells / span_mv / title
    if (!customElements.get("ge-cell-heatmap")) {
      customElements.define("ge-cell-heatmap", class extends HTMLElement {
        setConfig(cfg) {
          if (!cfg || !Array.isArray(cfg.batteries) || !cfg.batteries.length)
            throw new Error("ge-cell-heatmap: 'batteries: [serial, ...]' is required");
          this._cfg = cfg;
        }
        set hass(hass) { this._hass = hass; this._render(); }
        getCardSize() { return (this._cfg && this._cfg.batteries.length || 1) + 1; }

        _render() {
          const cfg = this._cfg, hass = this._hass;
          if (!hass || !hass.states) return;
          const nCells = cfg.cells || 16;
          // HA 2026.6+ prefixes entity_ids with the device's area slug
          // (e.g. "sensor.loft_givenergy_battery_..."). Resolve each canonical
          // id to the actual (possibly area-prefixed) id once and cache it on
          // the instance: `set hass` fires on every global state change, so a
          // full Object.entries(hass.states) scan per render would be costly on
          // large installs. A stale cache entry (entity_id changed) self-heals
          // - its state lookup misses, so we fall through and re-scan.
          this._cellIdCache = this._cellIdCache || {};
          const cellState = (s, n) => {
            const canonical = `sensor.givenergy_battery_${s.toLowerCase()}_cell_${n}_voltage`;
            const cached = this._cellIdCache[canonical];
            if (cached && hass.states[cached]) return hass.states[cached];
            if (hass.states[canonical]) {
              this._cellIdCache[canonical] = canonical;
              return hass.states[canonical];
            }
            for (const eid of Object.keys(hass.states)) {
              const i = eid.indexOf("givenergy_battery_");
              if (i > 0 && `sensor.${eid.slice(i)}` === canonical) {
                this._cellIdCache[canonical] = eid;
                return hass.states[eid];
              }
            }
            return null;
          };
          // `set hass` fires on every HA state change; skip the DOM rebuild
          // unless one of our cells (or the config) actually changed.
          const sig =
            (cfg.batteries || [])
              .map((s) => {
                const lo = s.toLowerCase();
                let cells = "";
                for (let n = 1; n <= nCells; n++) {
                  const st = cellState(lo, n);
                  cells += (st ? st.state : "?") + ",";
                }
                return lo + ":" + cells;
              })
              .join("|") +
            "#" + (cfg.title || "") + "/" + (cfg.span_mv != null ? cfg.span_mv : 15);
          if (sig === this._sig) return;
          this._sig = sig;
          const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
          const span = (cfg.span_mv != null ? cfg.span_mv : 15) / 1000;
          const valOf = (s, n) => {
            const st = cellState(s, n);
            const v = st ? Number(st.state) : NaN;
            return Number.isFinite(v) ? v : null;
          };
          const colour = (d) => {
            if (d == null) return "var(--disabled-color, #9e9e9e)";
            const t = Math.max(-1, Math.min(1, d / span));
            const f = Math.round(255 * (1 - Math.abs(t) * 0.85));
            return t >= 0 ? `rgb(255,${f},${f})` : `rgb(${f},${f},255)`;
          };
          const head = `<tr><th class="row">#</th>${Array.from({length: nCells}, (_, i) => `<th>${i + 1}</th>`).join("")}<th>m</th><th>&Delta;</th></tr>`;
          const rows = cfg.batteries.map((s, bi) => {
            const vals = Array.from({length: nCells}, (_, i) => valOf(s, i + 1));
            const present = vals.filter((v) => v != null);
            const mean = present.length ? present.reduce((a, b) => a + b, 0) / present.length : null;
            const dmv = present.length ? Math.round((Math.max(...present) - Math.min(...present)) * 1000) : null;
            const cells = vals.map((v) => {
              const d = (v != null && mean != null) ? v - mean : null;
              const dv = d != null ? Math.round(d * 1000) : null;
              const txt = dv != null ? (dv > 0 ? `+${dv}` : `${dv}`) : "";
              const title = v != null ? `${v.toFixed(3)} V` : "no data";
              return `<td title="${title}" style="background:${colour(d)}">${txt}</td>`;
            }).join("");
            const meanTxt = mean != null ? mean.toFixed(2) : "&mdash;";
            const dTxt = dmv != null ? `${dmv}` : "&mdash;";
            return `<tr><th class="row" title="${esc(s.toUpperCase())}">${bi + 1}</th>${cells}<td class="num">${meanTxt}</td><td class="num">${dTxt}</td></tr>`;
          }).join("");
          const packMap = cfg.batteries.map((s, bi) => `${bi + 1} = ${esc(s.toUpperCase())}`).join(" &middot; ");
          this.innerHTML = `
            <ha-card header="${esc(cfg.title || "Cell balance")}">
              <style>
                .wrap{padding:0 16px 16px}
                table{border-collapse:collapse;width:100%;font-size:12px;text-align:center;table-layout:fixed}
                th{font-weight:500;color:var(--secondary-text-color);padding:3px}
                th.row{width:1.6em;white-space:nowrap;text-align:right;padding:0 8px 0 0;font-weight:600}
                td{padding:7px 2px;color:#111;border:1px solid rgba(0,0,0,.08)}
                td.num{background:none;color:var(--primary-text-color);font-weight:600;white-space:nowrap}
                .legend{font-size:12px;color:var(--secondary-text-color);margin-top:10px;line-height:1.6}
                .sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin:0 3px}
              </style>
              <div class="wrap">
                <table><thead>${head}</thead><tbody>${rows}</tbody></table>
                <div class="legend"><b>Packs:</b> ${packMap}<br>
                  Colour = each cell's mV deviation from <b>its own pack's</b> mean (&plusmn;${cfg.span_mv != null ? cfg.span_mv : 15} mV scale) &mdash; imbalance shows regardless of charge level:<br>
                  <span class="sw" style="background:rgb(120,120,255)"></span> below &middot;
                  <span class="sw" style="background:#fff;border:1px solid #ccc"></span> mean &middot;
                  <span class="sw" style="background:rgb(255,120,120)"></span> above.<br>
                  <b>m</b> = pack mean cell voltage (V); <b>&Delta;</b> = spread (max&minus;min) in mV.</div>
              </div>
            </ha-card>`;
        }
      });
    }

    // custom:givenergy-flow - the Flow mode centrepiece. Three big-number
    // headers, an inline SVG power-flow diagram (edge direction follows the sign
    // of grid/battery power), and a today-totals strip. Entity_ids arrive
    // pre-resolved from the strategy; the card only reads hass.states. Responsive
    // via a container query so it works as a panel:true view and as a card slot.
    if (!customElements.get("givenergy-flow")) {
      customElements.define("givenergy-flow", class extends HTMLElement {
        setConfig(cfg) {
          this._cfg = cfg || {};
        }
        set hass(hass) {
          this._hass = hass;
          this._render();
        }
        getCardSize() {
          return 8;
        }

        _render() {
          var cfg = this._cfg, hass = this._hass;
          if (!cfg || !hass || !hass.states) return;
          var num = function (eid) {
            var st = eid && hass.states[eid];
            var v = st ? parseFloat(st.state) : NaN;
            return isFinite(v) ? v : null;
          };
          var esc = function (s) {
            return String(s).replace(/[&<>"']/g, function (c) {
              return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
            });
          };
          var fmtKw = function (w) {
            if (w == null) return "&mdash;";
            var k = w / 1000;
            return Math.abs(k) < 10 ? k.toFixed(2) : k.toFixed(1);
          };
          var fmtKwh = function (v) {
            if (v == null) return "&mdash;";
            return Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v).toString();
          };

          var solar = num(cfg.solar);
          var grid = num(cfg.grid); // + = export, - = import (per v1.1.3 rename)
          var load = num(cfg.load);
          var batt = num(cfg.battery_power); // + = discharge, - = charge
          var soc = num(cfg.battery_soc);
          var packs = (cfg.packs || []).map(function (p) {
            return { name: p.name, soc: num(p.soc) };
          });
          var totals = cfg.totals || {};

          // Skip the DOM rebuild unless a referenced value (or config) changed.
          var strings = (cfg.solar_strings || []).map(num);
          var sig = [solar, grid, load, batt, soc].join(",") +
            "|" + strings.join(",") +
            "|" + packs.map(function (p) { return p.name + ":" + p.soc; }).join(",") +
            "|" + Object.keys(totals).map(function (k) { return k + "=" + num(totals[k]); }).join(",");
          if (sig === this._sig) return;
          this._sig = sig;

          // Grid / battery direction sentences.
          var gridSub = "Idle";
          if (grid != null && Math.abs(grid) >= 10) {
            gridSub = grid > 0
              ? "Exporting " + fmtKw(grid) + " kW to grid"
              : "Importing " + fmtKw(-grid) + " kW from grid";
          }

          // ---- SVG flow diagram ----
          // Node centres in a 600x360 viewBox. Layout follows the HA Energy-flow
          // convention: grid on the left, home on the right, sources/sinks (solar,
          // battery) on the central vertical axis. Scales to more peripherals
          // without breaking, unlike a fixed circular arrangement.
          var N = {
            grid: { x: 80, y: 180 },
            home: { x: 520, y: 180 },
            solar: { x: 300, y: 70 },
            battery: { x: 300, y: 290 },
          };
          var R = 40; // node circle radius
          var HW = 52, HH = 33; // home box half-width / half-height
          var HOME_L = N.home.x - HW, HOME_R = N.home.x + HW;
          var HOME_T = N.home.y - HH, HOME_B = N.home.y + HH;
          var circ = 2 * Math.PI * R;

          var SOLAR = "#f5a623", EXPORT = "#5bbb6a", IMPORT = "#e5734d",
            CHARGE = "#4a9fd4", DISCHARGE = "#9b6dd4";
          var NEUTRAL = "var(--divider-color,#666)";

          // Node outline colour follows the dominant flow direction.
          var solarColor = (solar != null && solar > 20) ? SOLAR : NEUTRAL;
          var gridColor = (grid != null && grid > 20) ? EXPORT
            : (grid != null && grid < -20) ? IMPORT : NEUTRAL;
          var battColor = (batt != null && batt < -20) ? CHARGE
            : (batt != null && batt > 20) ? DISCHARGE : NEUTRAL;

          // Clip helpers: shorten bezier endpoints to node perimeters so edges
          // terminate cleanly at the circle or rectangle boundary.
          var clipCircle = function (fromX, fromY, cx, cy) {
            var dx = fromX - cx, dy = fromY - cy;
            var len = Math.sqrt(dx * dx + dy * dy) || 1;
            return { x: cx + dx / len * R, y: cy + dy / len * R };
          };
          var clipHome = function (ax, ay) {
            var bx = N.home.x, by = N.home.y;
            var dx = bx - ax, dy = by - ay;
            var best = Infinity, px = bx, py = by;
            var tryT = function (t) {
              if (t > 1e-6 && t < best) {
                var xx = ax + t * dx, yy = ay + t * dy;
                if (xx >= HOME_L - 1 && xx <= HOME_R + 1 && yy >= HOME_T - 1 && yy <= HOME_B + 1) {
                  best = t; px = xx; py = yy;
                }
              }
            };
            if (Math.abs(dx) > 0.1) { tryT((HOME_L - ax) / dx); tryT((HOME_R - ax) / dx); }
            if (Math.abs(dy) > 0.1) { tryT((HOME_T - ay) / dy); tryT((HOME_B - ay) / dy); }
            return { x: px, y: py };
          };
          // Quadratic bezier clipped to the source circle and destination perimeter.
          // Returns { d, mid } where d is the SVG path string and mid is the t=0.5 point
          // used to position edge labels.
          var curve = function (a, b, toHome) {
            var cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2 - 22;
            var s = clipCircle(b.x, b.y, a.x, a.y);
            var e = toHome ? clipHome(a.x, a.y) : clipCircle(a.x, a.y, b.x, b.y);
            return {
              d: "M" + s.x.toFixed(1) + " " + s.y.toFixed(1) +
                " Q " + cx.toFixed(1) + " " + cy.toFixed(1) +
                " " + e.x.toFixed(1) + " " + e.y.toFixed(1),
              mid: { x: 0.25 * s.x + 0.5 * cx + 0.25 * e.x,
                     y: 0.25 * s.y + 0.5 * cy + 0.25 * e.y }
            };
          };
          // Inline style for a live edge: animation speed and stroke width both
          // scale with flow magnitude (Watts). 0 W -> 1.5 s / 1.5 px; 10 kW -> 0.35 s / 4.5 px.
          var edgeStyle = function (pwr, color) {
            var p = Math.min(pwr, 10000);
            var dur = Math.max(0.35, 1.5 - p / 10000 * 1.15).toFixed(2);
            var sw = (1.5 + p / 10000 * 3).toFixed(1);
            return 'style="stroke:' + color + ';animation-duration:' + dur + 's;stroke-width:' + sw + '"';
          };
          // Flow decomposition (solar takes priority over grid as a source).
          // All values in Watts; signs follow: batt+ = discharge, grid+ = export.
          var THRESH = 20; // W -- below this a flow is considered zero / sensor noise
          var battCharge = (batt != null && batt < 0) ? -batt : 0;
          var battDischarge = (batt != null && batt > 0) ? batt : 0;
          var gridImport = (grid != null && grid < 0) ? -grid : 0;
          var gridExport = (grid != null && grid > 0) ? grid : 0;
          var solarGen = (solar != null && solar > 0) ? solar : 0;

          var flowSolarToBatt = Math.min(solarGen, battCharge);
          var flowGridToBatt = Math.max(0, battCharge - flowSolarToBatt);
          var solarAfterBatt = Math.max(0, solarGen - flowSolarToBatt);
          var flowSolarToGrid = Math.min(solarAfterBatt, gridExport);
          var flowBattToGrid = Math.max(0, gridExport - flowSolarToGrid);
          var flowSolarToHome = Math.max(0, solarAfterBatt - flowSolarToGrid);
          var flowGridToHome = Math.max(0, gridImport - flowGridToBatt);
          var flowBattToHome = Math.max(0, battDischarge - flowBattToGrid);
          // Home consumption derived from energy balance so it equals the sum of
          // all incoming flows displayed on the diagram, avoiding the slight drift
          // between the independent load sensor and the solar/grid/battery sensors.
          var homeDisplay = flowSolarToHome + flowGridToHome + flowBattToHome;

          // idleKey groups edges that share a physical connection (same curve,
          // possibly opposite directions) so only one idle path is rendered.
          // The two edges crossing the diagram centre (solar->battery vertical,
          // grid->home horizontal) share a midpoint, so each offsets its label
          // perpendicular to its own axis (labelDx / labelDy) to avoid the two
          // values colliding when both flows are active.
          var edges = [
            { c: curve(N.solar, N.home, true), on: flowSolarToHome > THRESH, color: SOLAR, pwr: flowSolarToHome, flow: flowSolarToHome, idleKey: 'sh' },
            { c: curve(N.solar, N.grid, false), on: flowSolarToGrid > THRESH, color: EXPORT, pwr: flowSolarToGrid, flow: flowSolarToGrid, idleKey: 'sg' },
            { c: curve(N.grid, N.home, true), on: flowGridToHome > THRESH, color: IMPORT, pwr: flowGridToHome, flow: flowGridToHome, labelDy: -26, idleKey: 'gh' },
            { c: curve(N.solar, N.battery, false), on: flowSolarToBatt > THRESH, color: CHARGE, pwr: flowSolarToBatt, flow: flowSolarToBatt, labelDx: -38, idleKey: 'sb' },
            { c: curve(N.battery, N.home, true), on: flowBattToHome > THRESH, color: DISCHARGE, pwr: flowBattToHome, flow: flowBattToHome, idleKey: 'bh' },
            { c: curve(N.grid, N.battery, false), on: flowGridToBatt > THRESH, color: CHARGE, pwr: flowGridToBatt, flow: flowGridToBatt, idleKey: 'gb' },
            { c: curve(N.battery, N.grid, false), on: flowBattToGrid > THRESH, color: DISCHARGE, pwr: flowBattToGrid, flow: flowBattToGrid, idleKey: 'gb' },
          ];
          // Active edges rendered first (animation + label), then a single idle
          // path per unique connection that has no active direction.
          var liveKeys = {};
          var edgeSvg = edges.map(function (e) {
            if (e.on) {
              liveKeys[e.idleKey] = true;
              var mx = (e.c.mid.x + (e.labelDx || 0)).toFixed(1), my = (e.c.mid.y + (e.labelDy || 0)).toFixed(1);
              return '<path class="edge live" ' + edgeStyle(e.pwr, e.color) + ' d="' + e.c.d + '"/>' +
                '<text class="e-label" x="' + mx + '" y="' + my + '" style="fill:' + e.color + '">' + fmtKw(e.flow) + ' kW</text>';
            }
            return null;
          }).filter(Boolean).join("");
          var seenIdle = {};
          edgeSvg += edges.map(function (e) {
            if (!e.on && !liveKeys[e.idleKey] && !seenIdle[e.idleKey]) {
              seenIdle[e.idleKey] = true;
              return '<path class="edge idle" d="' + e.c.d + '"/>';
            }
            return '';
          }).join("");

          var node = function (n, ring, label, value, unit, ringColor) {
            var rc = ringColor || NEUTRAL;
            var c = "";
            if (ring != null) {
              // SOC ring: a circle whose dash represents the percentage.
              var filled = circ * Math.max(0, Math.min(100, ring)) / 100;
              c = '<circle class="ring-bg" style="stroke:' + rc + '" cx="' + n.x + '" cy="' + n.y + '" r="' + R + '"/>' +
                '<circle class="ring-fg" cx="' + n.x + '" cy="' + n.y + '" r="' + R + '" ' +
                'stroke-dasharray="' + filled.toFixed(1) + " " + circ.toFixed(1) + '" ' +
                'transform="rotate(-90 ' + n.x + " " + n.y + ')"/>';
            } else {
              c = '<circle class="ring-bg" style="stroke:' + rc + '" cx="' + n.x + '" cy="' + n.y + '" r="' + R + '"/>';
            }
            return c +
              '<text class="n-label" x="' + n.x + '" y="' + (n.y - 10) + '">' + label + "</text>" +
              '<text class="n-value" x="' + n.x + '" y="' + (n.y + 15) + '">' + value +
              '<tspan class="n-unit"> ' + unit + "</tspan></text>";
          };
          var gridVal = grid == null ? "&mdash;" : fmtKw(Math.abs(grid));
          var nodesSvg =
            node(N.solar, null, "SOLAR", fmtKw(solar), "kW", solarColor) +
            node(N.grid, null, "GRID", gridVal, "kW", gridColor) +
            node(N.battery, soc, "BATTERY", soc == null ? "&mdash;" : Math.round(soc).toString(), "%", battColor) +
            '<rect class="home-box" x="' + HOME_L + '" y="' + HOME_T + '" width="' + (HW * 2) + '" height="' + (HH * 2) + '" rx="10"/>' +
            '<text class="n-label" x="' + N.home.x + '" y="' + (N.home.y - 10) + '">HOME</text>' +
            '<text class="n-value" x="' + N.home.x + '" y="' + (N.home.y + 15) + '">' + fmtKw(homeDisplay) +
            '<tspan class="n-unit"> kW</tspan></text>';

          // ---- header cards ----
          var strSub = strings.length
            ? strings.map(function (w, i) { return "String " + (i + 1) + " &middot; " + fmtKw(w) + " kW"; }).join("&nbsp;&nbsp;")
            : "&nbsp;";
          var packSub = packs.length
            ? packs.map(function (p) { return esc(p.name) + " &middot; " + (p.soc == null ? "&mdash;" : Math.round(p.soc) + "%"); }).join("&nbsp;&nbsp;")
            : "&nbsp;";

          // ---- totals strip ----
          var totalDefs = [
            { k: "pv_today", label: "PV TODAY", color: SOLAR },
            { k: "discharge_today", label: "DISCHARGE", color: DISCHARGE },
            { k: "import_today", label: "IMPORTED", color: IMPORT },
            { k: "house_today", label: "HOUSE", color: "#9e9e9e" },
            { k: "charge_today", label: "CHARGED", color: CHARGE },
            { k: "export_today", label: "EXPORTED", color: EXPORT },
          ];
          var totalsHtml = totalDefs
            .filter(function (t) { return totals[t.k]; })
            .map(function (t) {
              return '<div class="total"><div class="t-label"><span class="dot" style="background:' + t.color + '"></span>' +
                t.label + '</div><div class="t-val">' + fmtKwh(num(totals[t.k])) +
                '<span class="t-unit"> kWh</span></div></div>';
            }).join("");

          this.innerHTML =
            '<ha-card><div class="geflow">' +
            "<style>" +
            "@font-face{font-family:'GE Fraunces';src:url('/givenergy_local/fonts/fraunces-subset.woff2') format('woff2');font-weight:300 500;font-display:swap}" +
            "@font-face{font-family:'GE Geist Mono';src:url('/givenergy_local/fonts/geist-mono-subset.woff2') format('woff2');font-weight:400 600;font-display:swap}" +
            ".geflow{container-type:inline-size;padding:16px;color:var(--primary-text-color)}" +
            ".heads{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}" +
            ".head{background:var(--ha-card-background,var(--card-background-color,#1c1c1c));border:1px solid var(--divider-color,#333);border-radius:12px;padding:14px 16px}" +
            ".h-label{font-family:'GE Geist Mono',ui-monospace,monospace;font-size:13px;letter-spacing:.08em;color:var(--secondary-text-color);text-transform:uppercase}" +
            ".h-value{font-family:'GE Fraunces',Georgia,serif;font-size:43px;font-weight:300;line-height:1.2;margin-top:2px}" +
            ".h-value .u{font-family:'Roboto',system-ui,sans-serif;font-size:18px;color:var(--secondary-text-color);margin-left:4px}" +
            ".h-sub{font-size:12px;color:var(--secondary-text-color);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
            ".diagram{margin:8px 0}" +
            ".diagram svg{width:100%;height:auto;display:block}" +
            ".edge{fill:none;stroke-width:2.5}" +
            ".edge.idle{stroke:var(--divider-color,#999);opacity:.55;stroke-dasharray:3 7}" +
            ".edge.live{stroke-dasharray:5 9;animation:geflow-ants 0.9s linear infinite}" +
            "@keyframes geflow-ants{to{stroke-dashoffset:-14}}" +
            ".e-label{font-family:'GE Fraunces',Georgia,serif;font-size:11px;font-weight:300;text-anchor:middle;dominant-baseline:middle;paint-order:stroke fill;stroke:var(--ha-card-background,var(--card-background-color,#fff));stroke-width:5;stroke-linejoin:round}" +
            ".ring-bg{fill:none;stroke-width:3}" +
            ".ring-fg{fill:none;stroke:" + DISCHARGE + ";stroke-width:3;stroke-linecap:round}" +
            ".home-box{fill:none;stroke:var(--divider-color,#999);stroke-width:1.5}" +
            ".n-label{font-family:'GE Geist Mono',ui-monospace,monospace;fill:var(--secondary-text-color);font-size:13px;text-anchor:middle;letter-spacing:.06em}" +
            ".n-value{font-family:'GE Fraunces',Georgia,serif;fill:var(--primary-text-color);font-size:24px;font-weight:300;text-anchor:middle}" +
            ".n-unit{font-family:'Roboto',system-ui,sans-serif;fill:var(--secondary-text-color);font-size:12px}" +
            ".totals{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-top:8px}" +
            ".total{background:var(--ha-card-background,var(--card-background-color,#1c1c1c));border:1px solid var(--divider-color,#333);border-radius:10px;padding:10px 12px}" +
            ".t-label{font-family:'GE Geist Mono',ui-monospace,monospace;font-size:14px;letter-spacing:.06em;color:var(--secondary-text-color);text-transform:uppercase}" +
            ".dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}" +
            ".t-val{font-family:'GE Fraunces',Georgia,serif;font-size:31px;font-weight:300;margin-top:2px}" +
            ".t-unit{font-family:'Roboto',system-ui,sans-serif;font-size:16px;color:var(--secondary-text-color)}" +
            "@container (max-width:640px){.heads{grid-template-columns:1fr}.totals{grid-template-columns:repeat(3,1fr)}.h-value{font-size:28px}}" +
            "@container (max-width:380px){.totals{grid-template-columns:repeat(2,1fr)}}" +
            "</style>" +
            '<div class="heads">' +
            '<div class="head"><div class="h-label">Solar &middot; Now</div><div class="h-value">' + fmtKw(solar) + '<span class="u">kW</span></div><div class="h-sub">' + strSub + "</div></div>" +
            '<div class="head"><div class="h-label">Battery &middot; Combined SOC</div><div class="h-value">' + (soc == null ? "&mdash;" : Math.round(soc)) + '<span class="u">%</span></div><div class="h-sub">' + packSub + "</div></div>" +
            '<div class="head"><div class="h-label">Home &middot; Now</div><div class="h-value">' + fmtKw(load) + '<span class="u">kW</span></div><div class="h-sub">' + gridSub + "</div></div>" +
            "</div>" +
            '<div class="diagram"><svg viewBox="0 0 600 360" preserveAspectRatio="xMidYMid meet">' +
            edgeSvg + nodesSvg +
            "</svg></div>" +
            '<div class="totals">' + totalsHtml + "</div>" +
            "</div></ha-card>";
        }
      });
    }

    // custom:givenergy-glance - the Glance mode centrepiece. Calm-tech ambient
    // panel: a natural-language status sentence, three large numbers (solar today /
    // battery SOC / house today), and health pills. Entity_ids arrive pre-resolved
    // from the strategy; the card only reads hass.states.
    if (!customElements.get("givenergy-glance")) {
      customElements.define("givenergy-glance", class extends HTMLElement {
        setConfig(cfg) {
          this._cfg = cfg || {};
        }
        set hass(hass) {
          this._hass = hass;
          this._render();
        }
        getCardSize() {
          return 5;
        }

        _render() {
          var cfg = this._cfg, hass = this._hass;
          if (!cfg || !hass || !hass.states) return;

          var num = function (eid) {
            var st = eid && hass.states[eid];
            var v = st ? parseFloat(st.state) : NaN;
            return isFinite(v) ? v : null;
          };
          var esc = function (s) {
            return String(s).replace(/[&<>"']/g, function (c) {
              return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
            });
          };
          var fmtKw = function (w) {
            if (w == null) return "&mdash;";
            var k = w / 1000;
            return Math.abs(k) < 10 ? k.toFixed(2) : k.toFixed(1);
          };
          var fmtKwh = function (v) {
            if (v == null) return "&mdash;";
            return Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v).toString();
          };

          var solar = num(cfg.solar);
          var grid  = num(cfg.grid);   // + = export, - = import
          var batt  = num(cfg.battery_power); // + = discharge, - = charge
          var soc   = num(cfg.battery_soc);
          var load  = num(cfg.load);
          var strings = (cfg.solar_strings || []).map(num);
          var packs = (cfg.packs || []).map(function (p) {
            return { name: p.name, soc: num(p.soc) };
          });
          var totals = cfg.totals || {};

          // Signature guard: skip DOM rebuild if nothing changed.
          var sig = [solar, grid, batt, soc, load].join(",") +
            "|" + strings.join(",") +
            "|" + packs.map(function (p) { return p.name + ":" + p.soc; }).join(",") +
            "|" + Object.keys(totals).map(function (k) { return k + "=" + num(totals[k]); }).join(",");
          if (sig === this._sig) return;
          this._sig = sig;

          // Flow booleans with hysteresis (Schmitt-trigger style): THRESH_ON to
          // enter a state, THRESH_OFF to leave it. Prevents the sentence from
          // flipping between adjacent states when readings are near a threshold
          // (sensor timing skew, end-of-day grazing, etc.).
          var THRESH_ON  = 200; // W -- must exceed this to enter a new state
          var THRESH_OFF =  80; // W -- must drop below this to leave a state
          var prev = this._flowState || {};
          var solarOn     = solar != null && (solar >  THRESH_ON || (prev.solarOn     && solar >  THRESH_OFF));
          var exporting   = grid  != null && (grid  >  THRESH_ON || (prev.exporting   && grid  >  THRESH_OFF));
          var importing   = grid  != null && (grid  < -THRESH_ON || (prev.importing   && grid  < -THRESH_OFF));
          var charging    = batt  != null && (batt  < -THRESH_ON || (prev.charging    && batt  < -THRESH_OFF));
          var discharging = batt  != null && (batt  >  THRESH_ON || (prev.discharging && batt  >  THRESH_OFF));
          this._flowState = { solarOn: solarOn, exporting: exporting, importing: importing,
                              charging: charging, discharging: discharging };

          // Natural-language status sentence (ASCII-only).
          // Structure: check net grid direction (exporting/importing/idle) first
          // within the solarOn group so the sentence is correct even when solar
          // is present but insufficient to cover demand or the source of battery
          // charge is ambiguous.
          var sentence;
          if (solarOn) {
            if (exporting) {
              if (charging)
                sentence = "Solar covering the house and charging the battery, exporting the surplus.";
              else if (discharging)
                sentence = "Solar and battery exporting to the grid.";
              else
                sentence = "Solar ahead of demand - exporting to the grid.";
            } else if (importing) {
              if (charging)
                sentence = "Solar and grid supplying the house and charging the battery.";
              else if (discharging)
                sentence = "Solar, battery, and grid supplying the house.";
              else
                sentence = "Solar and grid supplying the house.";
            } else {
              // Grid approximately idle: solar balanced against load and battery.
              if (charging)
                sentence = "Solar covering the house and charging the battery.";
              else if (discharging)
                sentence = "Solar and battery covering the house.";
              else
                sentence = "Solar covering the house.";
            }
          } else if (discharging && !importing) {
            sentence = "Battery powering the house.";
          } else if (discharging && importing) {
            sentence = "Battery and grid supplying the house.";
          } else if (importing) {
            sentence = "Drawing from the grid.";
          } else {
            sentence = "System idle.";
          }

          // Status dot: amber when importing or battery low, green otherwise.
          var dotColor = (importing || (soc != null && soc < 20)) ? "#d4a85a" : "#5bbb6a";

          // ---- Big-3 sub-lines ----
          var solarSub;
          if (strings.length) {
            solarSub = strings.map(function (w, i) {
              return "String " + (i + 1) + ": " + (w == null ? "&mdash;" : fmtKw(w)) + " kW";
            }).join(" &middot; ");
          } else if (solar != null) {
            solarSub = fmtKw(solar) + " kW now";
          } else {
            solarSub = "&mdash;";
          }

          var battSub;
          if (packs.length > 1) {
            battSub = packs.map(function (p) {
              return esc(p.name) + ": " + (p.soc == null ? "&mdash;" : Math.round(p.soc) + "%");
            }).join(" &middot; ");
          } else if (charging) {
            battSub = "Charging &middot; " + fmtKw(-batt) + " kW";
          } else if (discharging) {
            battSub = "Discharging &middot; " + fmtKw(batt) + " kW";
          } else {
            battSub = "Idle";
          }

          var houseSub;
          if (exporting) {
            houseSub = "Exporting " + fmtKw(grid) + " kW";
          } else if (importing) {
            houseSub = "Importing " + fmtKw(-grid) + " kW";
          } else if (solar != null || batt != null) {
            houseSub = "Self-sufficient";
          } else {
            houseSub = "&mdash;";
          }

          // ---- Health pills ----
          var GREEN = "#5bbb6a", AMBER = "#d4a85a", BLUE = "#4a9fd4";
          var pills = [];
          // Battery count.
          if (packs.length > 0) {
            var battWord = packs.length === 1 ? "battery" : "batteries";
            pills.push({ label: packs.length + " " + battWord + " online", color: GREEN });
            // Per-pack SOC if more than one.
            if (packs.length > 1) {
              packs.forEach(function (p) {
                var pc = (p.soc != null && p.soc < 20) ? AMBER : GREEN;
                pills.push({ label: esc(p.name) + ": " + (p.soc == null ? "&mdash;" : Math.round(p.soc)) + "%", color: pc });
              });
            }
          } else if (soc != null) {
            pills.push({ label: "1 battery online", color: GREEN });
          }
          // Import / export today.
          var importKwh = num(totals.import_today);
          if (importKwh != null && importKwh >= 0.05) {
            pills.push({ label: fmtKwh(importKwh) + " kWh imported today", color: importKwh > 1 ? AMBER : BLUE });
          }
          var exportKwh = num(totals.export_today);
          if (exportKwh != null && exportKwh >= 0.05) {
            pills.push({ label: fmtKwh(exportKwh) + " kWh exported today", color: GREEN });
          }
          // Per-string generation pills (only when solar is active).
          if (solarOn && strings.length > 0) {
            strings.forEach(function (w, i) {
              if (w != null) {
                pills.push({ label: "String " + (i + 1) + ": " + fmtKw(w) + " kW", color: BLUE });
              }
            });
          }

          var pillsHtml = pills.map(function (p) {
            return '<span class="gl-pill"><span class="gl-dot" style="background:' + p.color + '"></span>' + p.label + '</span>';
          }).join("");

          // ---- Render ----
          var MONO = "'GE Geist Mono',ui-monospace,monospace";
          var SERIF = "'GE Fraunces',Georgia,serif";
          var SANS = "'Roboto',system-ui,sans-serif";

          this.innerHTML =
            '<ha-card><div class="gegl">' +
            "<style>" +
            "@font-face{font-family:'GE Fraunces';src:url('/givenergy_local/fonts/fraunces-subset.woff2') format('woff2');font-weight:300 500;font-display:swap}" +
            "@font-face{font-family:'GE Geist Mono';src:url('/givenergy_local/fonts/geist-mono-subset.woff2') format('woff2');font-weight:400 600;font-display:swap}" +
            ".gegl{container-type:inline-size;padding:28px 24px 20px;color:var(--primary-text-color)}" +
            ".gl-status{display:flex;align-items:flex-start;gap:12px;margin-bottom:32px}" +
            ".gl-dot-wrap{flex-shrink:0;padding-top:6px}" +
            ".gl-pulse{width:12px;height:12px;border-radius:50%;animation:gl-pulse 2.4s ease-in-out infinite}" +
            "@keyframes gl-pulse{0%,100%{opacity:1}50%{opacity:.3}}" +
            ".gl-sentence{font-family:" + SERIF + ";font-size:28px;font-weight:300;line-height:1.25;letter-spacing:-.01em;max-width:680px}" +
            ".gl-big3{display:grid;grid-template-columns:repeat(3,1fr);gap:32px;margin-bottom:28px}" +
            ".gl-num{border-top:1px solid var(--divider-color,#444);padding-top:16px}" +
            ".gl-lbl{font-family:" + MONO + ";font-size:11px;letter-spacing:.06em;color:var(--secondary-text-color);margin-bottom:6px}" +
            ".gl-val{font-family:" + SERIF + ";font-size:80px;font-weight:200;line-height:1;letter-spacing:-.03em}" +
            ".gl-unit{font-family:" + SANS + ";font-size:22px;font-weight:300;color:var(--secondary-text-color);margin-left:4px}" +
            ".gl-sub{font-family:" + MONO + ";font-size:12px;color:var(--secondary-text-color);margin-top:6px}" +
            ".gl-pills{display:flex;flex-wrap:wrap;gap:8px}" +
            ".gl-pill{display:inline-flex;align-items:center;gap:7px;padding:7px 13px;" +
              "border:1px solid var(--divider-color,#444);border-radius:999px;" +
              "font-family:" + MONO + ";font-size:12px;color:var(--primary-text-color)}" +
            ".gl-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}" +
            "@container (max-width:500px){.gl-sentence{font-size:20px}.gl-big3{grid-template-columns:1fr;gap:20px}.gl-val{font-size:56px}.gl-unit{font-size:16px}}" +
            "</style>" +
            '<div class="gl-status">' +
            '<div class="gl-dot-wrap"><div class="gl-pulse" style="background:' + dotColor + '"></div></div>' +
            '<div class="gl-sentence">' + sentence + '</div>' +
            '</div>' +
            '<div class="gl-big3">' +
            '<div class="gl-num"><div class="gl-lbl">SOLAR TODAY</div>' +
            '<div class="gl-val">' + fmtKwh(num(totals.pv_today)) + '<span class="gl-unit">kWh</span></div>' +
            '<div class="gl-sub">' + solarSub + '</div></div>' +
            '<div class="gl-num"><div class="gl-lbl">BATTERY</div>' +
            '<div class="gl-val">' + (soc == null ? "&mdash;" : Math.round(soc)) + '<span class="gl-unit">%</span></div>' +
            '<div class="gl-sub">' + battSub + '</div></div>' +
            '<div class="gl-num"><div class="gl-lbl">HOUSE TODAY</div>' +
            '<div class="gl-val">' + fmtKwh(num(totals.house_today)) + '<span class="gl-unit">kWh</span></div>' +
            '<div class="gl-sub">' + houseSub + '</div></div>' +
            '</div>' +
            '<div class="gl-pills">' + pillsHtml + '</div>' +
            '</div></ha-card>';
        }
      });
    }

    // custom:givenergy-analyst - the Analyst mode centrepiece. Dense terminal-
    // aesthetic card: live power metrics strip, energy ledger (sources vs sinks
    // with kWh and percentages), and an inverter diagnostics table.
    // Entity_ids arrive pre-resolved from the strategy; the card only reads hass.states.
    if (!customElements.get("givenergy-analyst")) {
      customElements.define("givenergy-analyst", class extends HTMLElement {
        setConfig(cfg) {
          this._cfg = cfg || {};
        }
        set hass(hass) {
          this._hass = hass;
          this._render();
        }
        getCardSize() {
          return 8;
        }

        _render() {
          var cfg = this._cfg, hass = this._hass;
          if (!cfg || !hass || !hass.states) return;

          var num = function (eid) {
            var st = eid && hass.states[eid];
            var v = st ? parseFloat(st.state) : NaN;
            return isFinite(v) ? v : null;
          };
          var str = function (eid) {
            var st = eid && hass.states[eid];
            return st ? st.state : null;
          };
          var esc = function (s) {
            return String(s).replace(/[&<>"']/g, function (c) {
              return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
            });
          };
          // fmtW: integer watts for |w| < 1000, else kW to 2dp.
          var fmtW = function (w) {
            if (w == null) return "&mdash;";
            var abs = Math.abs(w);
            if (abs < 1000) return Math.round(w) + " W";
            return (w / 1000).toFixed(2) + " kW";
          };
          var fmtKwh = function (v) {
            if (v == null) return "&mdash;";
            return Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v).toString();
          };

          var solar = num(cfg.solar);
          var grid  = num(cfg.grid);          // + = export, - = import
          var batt  = num(cfg.battery_power); // + = discharge, - = charge
          var soc   = num(cfg.battery_soc);
          var load  = num(cfg.load);
          var totals = cfg.totals || {};
          var diag   = cfg.diag   || {};

          // Signature guard: skip full re-render when nothing changed.
          var totVals = Object.keys(totals).map(function (k) { return num(totals[k]); }).join(",");
          var diagVals = Object.keys(diag).map(function (k) { return str(diag[k]); }).join(",");
          var sig = [solar, grid, batt, soc, load].join(",") + "|" + totVals + "|" + diagVals;
          if (sig === this._sig) return;
          this._sig = sig;

          // ---- live metrics strip ----
          var metricCell = function (label, value, sub, borderColor) {
            return '<div class="an-metric" style="border-left-color:' + borderColor + '">' +
              '<div class="an-m-lbl">' + label + '</div>' +
              '<div class="an-m-val">' + value + '</div>' +
              '<div class="an-m-sub">' + sub + '</div>' +
              '</div>';
          };

          // PV cell
          var pvVal = fmtW(solar);
          var pvSub = "";
          if (cfg.solar_strings && cfg.solar_strings.length) {
            pvSub = cfg.solar_strings.map(function (eid, i) {
              var v = num(eid);
              return "S" + (i + 1) + " " + (v == null ? "---" : Math.round(v) + " W");
            }).join(" / ");
          }

          // Battery cell
          var battAbs = batt == null ? null : Math.abs(batt);
          var battDir = batt == null ? "idle" :
            (batt < -10 ? "charging" : (batt > 10 ? "discharging" : "idle"));
          var battSub = battDir + (soc != null ? " | " + Math.round(soc) + "%" : "");
          var battColor = battDir === "charging"    ? "#4a9fd4" :
                          battDir === "discharging" ? "#5bbb6a" :
                                                      "var(--divider-color)";

          // Grid cell
          var gridAbs = grid == null ? null : Math.abs(grid);
          var gridDir = grid == null ? "idle" :
            (grid < -10 ? "importing" : (grid > 10 ? "exporting" : "idle"));
          var gridColor = gridDir === "exporting" ? "#5bbb6a" :
                          gridDir === "importing" ? "#e55555" :
                                                    "var(--divider-color)";

          var metricsHtml =
            metricCell("PV",      fmtW(solar),   pvSub,   "#d4a85a") +
            metricCell("LOAD",    fmtW(load),    "",      "var(--divider-color)") +
            metricCell("BATTERY", fmtW(battAbs), battSub, battColor) +
            metricCell("GRID",    fmtW(gridAbs), gridDir, gridColor);

          // ---- energy ledger ----
          var pvToday        = num(totals.pv_today);
          var dischargeToday = num(totals.discharge_today);
          var importToday    = num(totals.import_today);
          var houseToday     = num(totals.house_today);
          var chargeToday    = num(totals.charge_today);
          var exportToday    = num(totals.export_today);

          var sumSources = (pvToday || 0) + (dischargeToday || 0) + (importToday || 0);
          var sumSinks   = (houseToday || 0) + (chargeToday || 0) + (exportToday || 0);

          var pct = function (v, total) {
            if (v == null || !total) return "&mdash;";
            return Math.round(v / total * 100) + "%";
          };

          var ledgerRow = function (label, val, total) {
            return '<tr><td>' + label + '</td>' +
              '<td class="an-r">' + fmtKwh(val) + '</td>' +
              '<td class="an-r">' + pct(val, total) + '</td></tr>';
          };
          var ledgerTotalRow = function (label, val, color) {
            return '<tr class="an-tot" style="color:' + color + '">' +
              '<td>' + label + '</td>' +
              '<td class="an-r">' + fmtKwh(val || null) + '</td>' +
              '<td class="an-r">' + (val ? "100%" : "&mdash;") + '</td></tr>';
          };

          var ledgerHtml =
            '<table class="an-tbl">' +
            '<thead><tr><th>Source</th><th class="an-r">kWh</th><th class="an-r">%</th></tr></thead>' +
            '<tbody>' +
            ledgerRow("PV generation",     pvToday,        sumSources) +
            ledgerRow("Battery discharge", dischargeToday, sumSources) +
            ledgerRow("Grid import",       importToday,    sumSources) +
            ledgerTotalRow("Sources total", sumSources, "#d4a85a") +
            '</tbody></table>' +
            '<table class="an-tbl an-tbl-mt">' +
            '<thead><tr><th>Sink</th><th class="an-r">kWh</th><th class="an-r">%</th></tr></thead>' +
            '<tbody>' +
            ledgerRow("House consumption", houseToday,  sumSinks) +
            ledgerRow("Battery charge",    chargeToday, sumSinks) +
            ledgerRow("Grid export",       exportToday, sumSinks) +
            ledgerTotalRow("Sinks total", sumSinks, "#5bbb6a") +
            '</tbody></table>';

          // ---- diagnostics table ----
          var diagDefs = [
            { slot: "t_inverter_heatsink",  label: "Heatsink",        unit: " deg C", isNum: true  },
            { slot: "t_charger",            label: "Charger",          unit: " deg C", isNum: true  },
            { slot: "f_ac1",                label: "Grid freq",        unit: " Hz",    isNum: true  },
            // scale: 0.0001 is a stopgap (/10,000 only; correct formula is /10,000 - 1).
            // Tracked at dewet22/givenergy-modbus#209.
            { slot: "pf_inverter",          label: "Power factor",     unit: "",       isNum: true, scale: 0.0001 },
            { slot: "work_time_total",      label: "Work time",        unit: " h",     isNum: true  },
            { slot: "last_refresh",         label: "Last refresh",     unit: "",       isNum: false },
            { slot: "consecutive_failures", label: "Consec. failures", unit: "",       isNum: true  },
          ];
          var diagRowsHtml = diagDefs.map(function (d) {
            var eid = diag[d.slot];
            if (!eid) return "";
            var raw = d.isNum ? num(eid) : str(eid);
            if (raw != null && d.scale) raw = parseFloat((raw * d.scale).toFixed(4));
            var display = raw == null ? "&mdash;" : esc(String(raw)) + esc(d.unit);
            return '<tr><td>' + d.label + '</td><td class="an-r">' + display + '</td></tr>';
          }).join("");
          var diagHtml = diagRowsHtml
            ? '<table class="an-tbl an-diag">' +
              '<thead><tr><th>Diagnostic</th><th class="an-r">Value</th></tr></thead>' +
              '<tbody>' + diagRowsHtml + '</tbody></table>'
            : "";

          // ---- compose ----
          this.innerHTML =
            '<ha-card>' +
            '<style>' +
            ':host { display: block; }' +
            '.an-wrap { padding: 12px 16px; font-family: "GE Geist Mono", ui-monospace, monospace;' +
            '  container-type: inline-size; }' +
            '.an-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }' +
            '@container (max-width: 600px) { .an-metrics { grid-template-columns: repeat(2, 1fr); } }' +
            '@container (max-width: 300px) { .an-metrics { grid-template-columns: 1fr; } }' +
            '.an-metric { border-left: 3px solid var(--divider-color); padding: 6px 10px;' +
            '  background: var(--secondary-background-color); border-radius: 4px; }' +
            '.an-m-lbl { font-size: 10px; color: var(--secondary-text-color); letter-spacing: 0.08em; }' +
            '.an-m-val { font-size: 20px; font-weight: 600; color: var(--primary-text-color); line-height: 1.2; }' +
            '.an-m-sub { font-size: 11px; color: var(--secondary-text-color); }' +
            '.an-body { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }' +
            '@container (max-width: 600px) { .an-body { grid-template-columns: 1fr; } }' +
            '.an-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }' +
            '.an-tbl th { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;' +
            '  color: var(--secondary-text-color); padding: 0 4px 4px;' +
            '  border-bottom: 1px solid var(--divider-color); font-weight: 500; text-align: left; }' +
            '.an-tbl td { padding: 3px 4px; color: var(--primary-text-color); }' +
            '.an-tbl tbody tr:hover { background: var(--secondary-background-color); }' +
            '.an-r { text-align: right; }' +
            '.an-tot { font-weight: 600; border-top: 1px solid var(--divider-color); }' +
            '.an-tbl-mt { margin-top: 8px; }' +
            '.an-diag th { text-align: left; }' +
            '</style>' +
            '<div class="an-wrap">' +
            '<div class="an-metrics">' + metricsHtml + '</div>' +
            '<div class="an-body">' +
            '<div>' + ledgerHtml + '</div>' +
            (diagHtml ? '<div>' + diagHtml + '</div>' : '') +
            '</div>' +
            '</div>' +
            '</ha-card>';
        }
      });
    }

    // Discoverability in the "Community dashboards" picker (HA 2026.5+). Harmless
    // where unsupported.
    try {
      window.customStrategies = window.customStrategies || [];
      window.customStrategies.push({
        type: "givenergy",
        strategyType: "dashboard",
        name: "GivEnergy",
        description: "Registry-driven GivEnergy dashboard (classic / flow / glance / analyst / all modes).",
      });
    } catch (e) {
      /* non-fatal */
    }
  }

  // Node (vitest): export the builders for unit testing.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
