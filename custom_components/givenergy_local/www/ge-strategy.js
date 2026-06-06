// GivEnergy dashboard strategy (bundled with the givenergy_local integration
// and auto-registered as a frontend module - no manual install).
//
// Registers a Lovelace *dashboard strategy* `custom:givenergy` that generates
// the dashboard from the live registry on every render, so it never goes stale.
// v1 ships `mode: classic` - a faithful reproduction of the six-tab dashboard
// the `givenergy_local.generate_dashboard` service emits as static YAML, but
// resolved from the registry instead of frozen entity-id strings.
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
      target = inverters[0] || null;
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
    // Flow is inverter-centric; an EMS plant has no PV/battery/grid flow, so
    // fall back to the classic (EMS) view set with no flow panel.
    if (plant.target && plant.target.isEms) return classicViews(plant, opts);
    return [flowView(plant, a, opts)].concat(classicViews(plant, opts));
  }

  function flowView(plant, a, opts) {
    var cfg = { type: "custom:givenergy-flow", max_power_kw: opts.maxPowerKw || 10 };
    if (a.inv("p_pv")) cfg.solar = a.inv("p_pv");
    var strings = [a.inv("p_pv1"), a.inv("p_pv2")].filter(Boolean);
    if (strings.length) cfg.solar_strings = strings;
    if (a.inv("grid_power")) cfg.grid = a.inv("grid_power");
    if (a.inv("p_load_demand")) cfg.load = a.inv("p_load_demand");
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
      if (a.inv("grid_power")) ents.grid = { entity: a.inv("grid_power") };
      if (a.inv("p_load_demand")) ents.home = { entity: a.inv("p_load_demand") };
      cards.push({ type: "custom:power-flow-card-plus", entities: ents });
    } else {
      cards.push(placeholder("power-flow-card-plus"));
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
      { entity: a.inv("p_load_demand"), name: "Load", color: "#EF5350" },
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
          name: "Regenerate Dashboard",
          icon: "mdi:view-dashboard-refresh",
          action_name: "Run",
          tap_action: {
            action: "perform-action",
            perform_action: "givenergy_local.generate_dashboard",
            data: { max_power_kw: opts.maxPowerKw || 10 },
          },
        },
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
    var views = opts.mode === "flow" ? flowViews(plant, opts) : classicViews(plant, opts);
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
    // custom:ge-cell-heatmap — merged from ge-cell-heatmap.js.
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
          var sig = [solar, grid, load, batt, soc].join(",") +
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
          // Node centres in a 600x320 viewBox.
          var N = {
            solar: { x: 110, y: 84 },
            grid: { x: 490, y: 84 },
            battery: { x: 490, y: 238 },
            home: { x: 300, y: 250 },
          };
          var curve = function (a, b) {
            var cx = (a.x + b.x) / 2;
            var cy = (a.y + b.y) / 2 - 22;
            return "M" + a.x + " " + a.y + " Q " + cx + " " + cy + " " + b.x + " " + b.y;
          };
          var SOLAR = "#f5a623", EXPORT = "#5bbb6a", IMPORT = "#e5734d",
            CHARGE = "#4a9fd4", DISCHARGE = "#9b6dd4";
          var edges = [
            { d: curve(N.solar, N.home), on: solar != null && solar > 20, color: SOLAR },
            { d: curve(N.solar, N.grid), on: grid != null && grid > 20, color: EXPORT },
            { d: curve(N.grid, N.home), on: grid != null && grid < -20, color: IMPORT },
            { d: curve(N.solar, N.battery), on: batt != null && batt < -20, color: CHARGE },
            { d: curve(N.battery, N.home), on: batt != null && batt > 20, color: DISCHARGE },
          ];
          var edgeSvg = edges.map(function (e) {
            if (e.on) {
              return '<path class="edge live" style="stroke:' + e.color + '" d="' + e.d + '"/>';
            }
            return '<path class="edge idle" d="' + e.d + '"/>';
          }).join("");

          var node = function (n, ring, label, value, unit) {
            var c = "";
            if (ring != null) {
              // SOC ring: a circle whose dash represents the percentage.
              var r = 40, circ = 2 * Math.PI * r;
              var filled = circ * Math.max(0, Math.min(100, ring)) / 100;
              c = '<circle class="ring-bg" cx="' + n.x + '" cy="' + n.y + '" r="' + r + '"/>' +
                '<circle class="ring-fg" cx="' + n.x + '" cy="' + n.y + '" r="' + r + '" ' +
                'stroke-dasharray="' + filled.toFixed(1) + " " + circ.toFixed(1) + '" ' +
                'transform="rotate(-90 ' + n.x + " " + n.y + ')"/>';
            } else {
              c = '<circle class="ring-bg" cx="' + n.x + '" cy="' + n.y + '" r="40"/>';
            }
            return c +
              '<text class="n-label" x="' + n.x + '" y="' + (n.y - 8) + '">' + label + "</text>" +
              '<text class="n-value" x="' + n.x + '" y="' + (n.y + 12) + '">' + value +
              '<tspan class="n-unit"> ' + unit + "</tspan></text>";
          };
          var gridVal = grid == null ? "&mdash;" : (grid > 0 ? "+" : "") + fmtKw(grid);
          var nodesSvg =
            node(N.solar, null, "SOLAR", fmtKw(solar), "kW") +
            node(N.grid, null, "GRID", gridVal, "kW") +
            node(N.battery, soc, "BATTERY", soc == null ? "&mdash;" : Math.round(soc).toString(), "%") +
            '<rect class="home-box" x="' + (N.home.x - 52) + '" y="' + (N.home.y - 30) + '" width="104" height="60" rx="8"/>' +
            '<text class="n-label" x="' + N.home.x + '" y="' + (N.home.y - 6) + '">HOME</text>' +
            '<text class="n-value" x="' + N.home.x + '" y="' + (N.home.y + 14) + '">' + fmtKw(load) +
            '<tspan class="n-unit"> kW</tspan></text>';

          // ---- header cards ----
          var strings = (cfg.solar_strings || []).map(num);
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
            ".geflow{container-type:inline-size;padding:16px;color:var(--primary-text-color)}" +
            ".heads{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}" +
            ".head{background:var(--ha-card-background,var(--card-background-color,#1c1c1c));border:1px solid var(--divider-color,#333);border-radius:12px;padding:14px 16px}" +
            ".h-label{font-size:11px;letter-spacing:.08em;color:var(--secondary-text-color);text-transform:uppercase}" +
            ".h-value{font-size:34px;font-weight:300;line-height:1.2;margin-top:2px}" +
            ".h-value .u{font-size:15px;color:var(--secondary-text-color);margin-left:4px}" +
            ".h-sub{font-size:12px;color:var(--secondary-text-color);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
            ".diagram{margin:8px 0}" +
            ".diagram svg{width:100%;height:auto;display:block}" +
            ".edge{fill:none;stroke-width:2.5}" +
            ".edge.idle{stroke:var(--divider-color,#444);opacity:.35;stroke-dasharray:2 6}" +
            ".edge.live{stroke-dasharray:5 9;animation:geflow-ants 0.9s linear infinite}" +
            "@keyframes geflow-ants{to{stroke-dashoffset:-14}}" +
            ".ring-bg{fill:none;stroke:var(--divider-color,#444);stroke-width:3}" +
            ".ring-fg{fill:none;stroke:" + DISCHARGE + ";stroke-width:3;stroke-linecap:round}" +
            ".home-box{fill:var(--ha-card-background,#1c1c1c);stroke:var(--divider-color,#444);stroke-width:1.5}" +
            ".n-label{fill:var(--secondary-text-color);font-size:11px;text-anchor:middle;letter-spacing:.06em}" +
            ".n-value{fill:var(--primary-text-color);font-size:20px;font-weight:300;text-anchor:middle}" +
            ".n-unit{fill:var(--secondary-text-color);font-size:11px}" +
            ".totals{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-top:8px}" +
            ".total{background:var(--ha-card-background,var(--card-background-color,#1c1c1c));border:1px solid var(--divider-color,#333);border-radius:10px;padding:10px 12px}" +
            ".t-label{font-size:10px;letter-spacing:.06em;color:var(--secondary-text-color);text-transform:uppercase}" +
            ".dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}" +
            ".t-val{font-size:20px;font-weight:300;margin-top:2px}" +
            ".t-unit{font-size:11px;color:var(--secondary-text-color)}" +
            "@container (max-width:640px){.heads{grid-template-columns:1fr}.totals{grid-template-columns:repeat(3,1fr)}.h-value{font-size:28px}}" +
            "@container (max-width:380px){.totals{grid-template-columns:repeat(2,1fr)}}" +
            "</style>" +
            '<div class="heads">' +
            '<div class="head"><div class="h-label">Solar &middot; Now</div><div class="h-value">' + fmtKw(solar) + '<span class="u">kW</span></div><div class="h-sub">' + strSub + "</div></div>" +
            '<div class="head"><div class="h-label">Battery &middot; Combined SOC</div><div class="h-value">' + (soc == null ? "&mdash;" : Math.round(soc)) + '<span class="u">%</span></div><div class="h-sub">' + packSub + "</div></div>" +
            '<div class="head"><div class="h-label">Home &middot; Now</div><div class="h-value">' + fmtKw(load) + '<span class="u">kW</span></div><div class="h-sub">' + gridSub + "</div></div>" +
            "</div>" +
            '<div class="diagram"><svg viewBox="0 0 600 320" preserveAspectRatio="xMidYMid meet">' +
            edgeSvg + nodesSvg +
            "</svg></div>" +
            '<div class="totals">' + totalsHtml + "</div>" +
            "</div></ha-card>";
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
        description: "Registry-driven GivEnergy dashboard (classic / flow modes).",
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
