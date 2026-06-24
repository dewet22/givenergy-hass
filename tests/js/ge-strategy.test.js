// Unit tests for the GivEnergy dashboard strategy (classic mode). Mirrors the
// structural-parity discipline of tests/test_dashboard.py on the JS side, and
// guards the registry-resolution behaviour that fixes the dangling-ids bug.

const path = require("path");
const { makeHass } = require("./mock-hass");

// Require once, before any customElements stub exists, so the module's
// browser-registration branch is skipped and only the Node exports are taken.
const GE = require(
  path.join(__dirname, "..", "..", "custom_components", "givenergy_local", "www", "ge-strategy.js")
);

// --- helpers ----------------------------------------------------------------

async function withCards(names, fn) {
  const reg = new Set(names);
  global.customElements = { get: (n) => (reg.has(n) ? class {} : undefined) };
  try {
    return await fn();
  } finally {
    delete global.customElements;
  }
}

function collectRefs(node, out) {
  out = out || [];
  if (Array.isArray(node)) {
    node.forEach((n) => collectRefs(n, out));
  } else if (node && typeof node === "object") {
    for (const k of Object.keys(node)) {
      const v = node[k];
      if ((k === "entity" || k === "state_of_charge") && typeof v === "string") out.push(v);
      else collectRefs(v, out);
    }
  }
  return out;
}

function hasNullEntity(node) {
  if (Array.isArray(node)) return node.some(hasNullEntity);
  if (node && typeof node === "object") {
    if ("entity" in node && node.entity === null) return true;
    return Object.keys(node).some((k) => hasNullEntity(node[k]));
  }
  return false;
}

async function regSet(hass) {
  const ents = await hass.callWS({ type: "config/entity_registry/list" });
  return new Set(ents.filter((e) => e.platform === "givenergy_local").map((e) => e.entity_id));
}

const titles = (dash) => dash.views.map((v) => v.title);
const view = (dash, title) => dash.views.find((v) => v.title === title);
const card = (v, pred) => (v.cards || []).find(pred);
const byTitle = (t) => (c) => c.title === t;

// --- tests ------------------------------------------------------------------

describe("classic dashboard structure", () => {
  it("emits the six classic views for an inverter + battery plant", async () => {
    const hass = makeHass({ batterySerials: ["BAT1", "BAT2"], acCoupled: true });
    const dash = await GE.generateDashboard({ mode: "classic" }, hass);
    expect(titles(dash)).toEqual([
      "Overview",
      "Energy",
      "Batteries",
      "Battery Health",
      "Controls",
      "Diagnostics",
    ]);
  });

  it("omits Battery Health when there are no batteries", async () => {
    const hass = makeHass({ batterySerials: [] });
    const dash = await GE.generateDashboard({}, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "Batteries", "Controls", "Diagnostics"]);
  });

  it("renders one Batteries section per pack", async () => {
    const hass = makeHass({ batterySerials: ["BAT1", "BAT2", "BAT3"] });
    const dash = await GE.generateDashboard({}, hass);
    expect(view(dash, "Batteries").sections.length).toBe(3);
  });
});

describe("registry resolution", () => {
  it("resolves every entity from the registry and survives the loft_ area prefix", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], acCoupled: true, areaPrefix: "loft_" });
    const dash = await GE.generateDashboard({}, hass);
    const refs = collectRefs(dash);
    const registry = await regSet(hass);

    expect(refs.length).toBeGreaterThan(50);
    for (const r of refs) {
      expect(registry.has(r)).toBe(true); // came from the registry, not constructed
      expect(r).toContain("loft_"); // proves the current (area-prefixed) id was read
    }
  });

  it("ignores entities from other integrations", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"] });
    const dash = await GE.generateDashboard({}, hass);
    expect(collectRefs(dash)).not.toContain("sensor.kitchen_temperature");
  });

  it("omits a missing entity gracefully instead of leaving a dangling row", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], omitKeys: ["p_backup", "t_charger"] });
    const dash = await GE.generateDashboard({}, hass);
    expect(hasNullEntity(dash)).toBe(false);

    const diag = view(dash, "Diagnostics");
    const names = (c) => (c.entities || []).map((e) => e.name);
    expect(names(card(diag, byTitle("Electrical")))).not.toContain("Backup Power");
    expect(names(card(diag, byTitle("Temperatures")))).not.toContain("Charger");
  });
});

describe("strategy options", () => {
  it("honours max_power_kw on the Overview 24h chart", async () => {
    await withCards(["power-flow-card-plus", "apexcharts-card"], async () => {
      const hass = makeHass({ batterySerials: ["BAT1"] });
      const dash = await GE.generateDashboard({ max_power_kw: 7 }, hass);
      const chart = card(
        view(dash, "Overview"),
        (c) => c.type === "custom:apexcharts-card"
      );
      expect(chart.yaxis[0]).toEqual({ min: -7000, max: 7000 });
    });
  });

  it("defaults the chart envelope to +/-10 kW", async () => {
    await withCards(["apexcharts-card"], async () => {
      const hass = makeHass({});
      const dash = await GE.generateDashboard({}, hass);
      const chart = card(view(dash, "Overview"), (c) => c.type === "custom:apexcharts-card");
      expect(chart.yaxis[0]).toEqual({ min: -10000, max: 10000 });
    });
  });

  it("selects the pinned serial among multiple plants", async () => {
    const hass = makeHass({ inverterSerial: "INV123", extraInverterSerial: "INV999" });
    const dash = await GE.generateDashboard({ serial: "INV999" }, hass);
    const refs = collectRefs(dash);
    expect(refs.some((r) => r.includes("inv999"))).toBe(true);
    expect(refs.some((r) => r.includes("inv123"))).toBe(false);
  });

  it("defaults to the first plant (sorted) when no serial is pinned", async () => {
    const hass = makeHass({ inverterSerial: "INV123", extraInverterSerial: "INV999" });
    const dash = await GE.generateDashboard({}, hass);
    const refs = collectRefs(dash);
    expect(refs.some((r) => r.includes("inv123"))).toBe(true);
    expect(refs.some((r) => r.includes("inv999"))).toBe(false);
  });
});

describe("feature detection", () => {
  it("falls back to a markdown placeholder when power-flow / apexcharts are absent", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"] });
    const dash = await GE.generateDashboard({}, hass); // no customElements stub
    const overview = view(dash, "Overview");
    expect(overview.cards[0].type).toBe("markdown");
    expect(overview.cards[0].content).toContain("power-flow-card-plus");
  });

  it("uses the real cards when registered", async () => {
    await withCards(["power-flow-card-plus", "apexcharts-card"], async () => {
      const hass = makeHass({ batterySerials: ["BAT1"] });
      const dash = await GE.generateDashboard({}, hass);
      const overview = view(dash, "Overview");
      expect(overview.cards[0].type).toBe("custom:power-flow-card-plus");
      expect(card(overview, (c) => c.type === "custom:apexcharts-card")).toBeTruthy();
    });
  });

  it("inverts the grid sign for power-flow-card-plus (grid_power is +ve=export) (#212)", async () => {
    await withCards(["power-flow-card-plus"], async () => {
      const hass = makeHass({ batterySerials: ["BAT1"] });
      const dash = await GE.generateDashboard({}, hass);
      const overview = view(dash, "Overview");
      const flow = card(overview, (c) => c.type === "custom:power-flow-card-plus");
      // The card defaults to +ve=import; grid_power is +ve=export, so without the
      // invert the grid bubble shows import as export and throws off the home flow.
      expect(flow.entities.grid.invert_state).toBe(true);
    });
  });

  it("shows AC-coupled and Smart Load cards only when those entities exist", async () => {
    const plain = await GE.generateDashboard({}, makeHass({ smartLoad: false }));
    const controls = view(plain, "Controls");
    expect(card(controls, byTitle("AC-Coupled"))).toBeUndefined();
    expect(card(controls, byTitle("Smart Load"))).toBeUndefined();

    const full = await GE.generateDashboard({}, makeHass({ acCoupled: true, smartLoad: true }));
    const controls2 = view(full, "Controls");
    expect(card(controls2, byTitle("AC-Coupled"))).toBeTruthy();
    expect(card(controls2, byTitle("Smart Load"))).toBeTruthy();
  });
});

describe("EMS plant", () => {
  it("emits telemetry views plus the EMS controls/diagnostics set", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({}, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "EMS Controls", "Diagnostics"]);

    const plant = card(view(dash, "EMS Controls"), byTitle("Plant"));
    const refs = collectRefs(plant);
    expect(refs.some((r) => r.endsWith("ems_plant_enable"))).toBe(true);
  });

  it("surfaces the controller's PV/grid/battery and the EMS load on the Overview", async () => {
    await withCards(["power-flow-card-plus"], async () => {
      const hass = makeHass({ ems: true });
      const dash = await GE.generateDashboard({}, hass);
      const overview = view(dash, "Overview");

      const flow = card(overview, (c) => c.type === "custom:power-flow-card-plus");
      const refs = collectRefs(flow);
      expect(refs.some((r) => r.endsWith("_p_pv"))).toBe(true);
      expect(refs.some((r) => r.endsWith("_grid_power"))).toBe(true);
      expect(refs.some((r) => r.endsWith("_p_battery"))).toBe(true);
      // Load uses the EMS aggregate (p_load_demand is gated off on the controller).
      expect(refs.some((r) => r.endsWith("_ems_calc_load_power"))).toBe(true);

      const emsPlant = card(overview, byTitle("EMS Plant"));
      expect(emsPlant).toBeTruthy();
      expect(collectRefs(emsPlant).some((r) => r.endsWith("_ems_grid_meter_power"))).toBe(true);
    });
  });
});

describe("EMS target selection", () => {
  it("prefers the EMS controller over a lower-sorting plain inverter", async () => {
    const hass = makeHass({ ems: true, inverterSerial: "ZZZ999", extraInverterSerial: "AAA111" });
    const plant = await GE.buildPlant(hass, {});
    expect(plant.target.isEms).toBe(true);
    expect(plant.target.serial).toBe("ZZZ999");
  });

  it("still honours an explicit serial pin to a plain inverter on an EMS plant", async () => {
    const hass = makeHass({ ems: true, inverterSerial: "ZZZ999", extraInverterSerial: "AAA111" });
    const plant = await GE.buildPlant(hass, { serial: "AAA111" });
    expect(plant.target.serial).toBe("AAA111");
    expect(plant.target.isEms).toBe(false);
  });
});

describe("no GivEnergy plant", () => {
  it("returns a friendly notice rather than throwing", async () => {
    const empty = {
      callWS: (msg) =>
        Promise.resolve(msg.type === "config/entity_registry/list" ? [] : []),
    };
    const dash = await GE.generateDashboard({}, empty);
    expect(dash.views[0].cards[0].type).toBe("markdown");
    expect(dash.views[0].cards[0].content).toContain("No GivEnergy plant");
  });
});

describe("multi-plant safety", () => {
  it("does not borrow another plant's batteries when the target has none", async () => {
    // INV123 has no batteries; INV999 owns BAT9 via via_device. With no serial
    // pin the target is INV123 (sorted first) - its battery match is genuinely
    // empty and must stay empty rather than showing INV999's pack.
    const hass = makeHass({
      inverterSerial: "INV123",
      batterySerials: [],
      extraInverterSerial: "INV999",
      extraBatterySerials: ["BAT9"],
    });
    const dash = await GE.generateDashboard({}, hass);
    expect(titles(dash)).not.toContain("Battery Health");
    expect(collectRefs(dash).some((r) => r.includes("bat9"))).toBe(false);
  });

  it("shows the no-plant notice (naming the serial) for an unmatched pin", async () => {
    // A typo'd / stale serial pin must NOT silently fall back to another plant,
    // or the Maintenance buttons would target the wrong inverter.
    const hass = makeHass({ inverterSerial: "INV123", extraInverterSerial: "INV999" });
    const dash = await GE.generateDashboard({ serial: "NOPE404" }, hass);
    expect(dash.views[0].cards[0].type).toBe("markdown");
    expect(dash.views[0].cards[0].content).toContain("NOPE404");
    expect(collectRefs(dash).some((r) => r.includes("inv123") || r.includes("inv999"))).toBe(false);
  });

  it("still classifies an inverter when its marker entity is disabled", async () => {
    // Disabling p_pv (the inverter marker) must not make the whole device
    // vanish - classification uses the full key set, not just enabled entities.
    const hass = makeHass({ batterySerials: ["BAT1"], disabledKeys: ["p_pv"] });
    const dash = await GE.generateDashboard({}, hass);
    expect(titles(dash)).toContain("Overview"); // inverter did not disappear
    // ...but the disabled entity itself is never rendered.
    expect(collectRefs(dash).some((r) => r.endsWith("_p_pv"))).toBe(false);
  });
});

describe("registry read failure", () => {
  it("returns a friendly notice instead of crashing the render", async () => {
    const failing = { callWS: () => Promise.reject(new Error("disconnected")) };
    const dash = await GE.generateDashboard({}, failing);
    expect(dash.views[0].cards[0].type).toBe("markdown");
    expect(dash.views[0].cards[0].content).toContain("Could not read the entity registry");
  });
});

describe("flow mode", () => {
  const flowCard = (dash) => view(dash, "Flow").cards[0];

  it("leads with a panel Flow view, then the full classic view set", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], acCoupled: true });
    const dash = await GE.generateDashboard({ mode: "flow" }, hass);
    expect(titles(dash)).toEqual([
      "Flow",
      "Overview",
      "Energy",
      "Batteries",
      "Battery Health",
      "Controls",
      "Diagnostics",
    ]);
    const flow = view(dash, "Flow");
    expect(flow.panel).toBe(true);
    expect(flow.cards.length).toBe(1);
    expect(flow.cards[0].type).toBe("custom:givenergy-flow");
  });

  it("resolves every flow slot from the registry and survives the loft_ prefix", async () => {
    const hass = makeHass({ batterySerials: ["BAT1", "BAT2"], areaPrefix: "loft_" });
    const dash = await GE.generateDashboard({ mode: "flow" }, hass);
    const c = flowCard(dash);
    const registry = await regSet(hass);

    const slots = [c.solar, c.grid, c.load, c.battery_power, c.battery_soc]
      .concat(c.solar_strings || [])
      .concat(Object.values(c.totals || {}))
      .concat((c.packs || []).map((p) => p.soc));

    expect(slots.length).toBeGreaterThan(10);
    for (const eid of slots) {
      expect(registry.has(eid)).toBe(true); // resolved, not constructed
      expect(eid).toContain("loft_"); // current area-prefixed id was read
    }
    expect((c.packs || []).map((p) => p.name)).toEqual(["BAT1", "BAT2"]);
  });

  it("omits a slot whose entity is missing rather than emitting null", async () => {
    // Omit non-marker flow slots (p_pv is the inverter classify marker, so it
    // must stay for the plant to resolve at all).
    const hass = makeHass({ batterySerials: ["BAT1"], omitKeys: ["p_load_demand", "e_grid_out_day"] });
    const c = flowCard(await GE.generateDashboard({ mode: "flow" }, hass));
    expect(c.load).toBeUndefined();
    expect(c.totals.export_today).toBeUndefined();
    expect(c.solar).toBeTruthy(); // unaffected slots still present
    expect(c.totals.pv_today).toBeTruthy();
    expect(hasNullEntity(c)).toBe(false);
  });

  it("adds kiosk-mode hints only when kiosk-mode is registered", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"] });
    const without = await GE.generateDashboard({ mode: "flow" }, hass);
    expect(view(without, "Flow").kiosk_mode).toBeUndefined();

    await withCards(["kiosk-mode"], async () => {
      const hass2 = makeHass({ batterySerials: ["BAT1"] });
      const withKiosk = await GE.generateDashboard({ mode: "flow" }, hass2);
      expect(view(withKiosk, "Flow").kiosk_mode).toEqual({
        hide_header: true,
        hide_sidebar: true,
      });
    });
  });

  it("falls back to classic for an unknown mode", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"] });
    const dash = await GE.generateDashboard({ mode: "nonsense" }, hass);
    expect(titles(dash)[0]).toBe("Overview");
    expect(view(dash, "Flow")).toBeUndefined();
  });

  it("falls back to the classic EMS view set (no Flow panel) for an EMS plant", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({ mode: "flow" }, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "EMS Controls", "Diagnostics"]);
    expect(view(dash, "Flow")).toBeUndefined();
  });
});

describe("glance mode", () => {
  const glanceCard = (dash) => view(dash, "Glance").cards[0];

  it("leads with a panel Glance view, then the full classic view set", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], acCoupled: true });
    const dash = await GE.generateDashboard({ mode: "glance" }, hass);
    expect(titles(dash)).toEqual([
      "Glance",
      "Overview",
      "Energy",
      "Batteries",
      "Battery Health",
      "Controls",
      "Diagnostics",
    ]);
    const gl = view(dash, "Glance");
    expect(gl.panel).toBe(true);
    expect(gl.cards.length).toBe(1);
    expect(gl.cards[0].type).toBe("custom:givenergy-glance");
  });

  it("resolves every glance slot from the registry and survives the loft_ prefix", async () => {
    const hass = makeHass({ batterySerials: ["BAT1", "BAT2"], areaPrefix: "loft_" });
    const dash = await GE.generateDashboard({ mode: "glance" }, hass);
    const c = glanceCard(dash);
    const registry = await regSet(hass);

    const slots = [c.solar, c.grid, c.load, c.battery_power, c.battery_soc]
      .concat(c.solar_strings || [])
      .concat(Object.values(c.totals || {}))
      .concat((c.packs || []).map((p) => p.soc));

    expect(slots.length).toBeGreaterThan(8);
    for (const eid of slots) {
      expect(registry.has(eid)).toBe(true);
      expect(eid).toContain("loft_");
    }
    expect((c.packs || []).map((p) => p.name)).toEqual(["BAT1", "BAT2"]);
  });

  it("omits totals whose entities are missing rather than emitting null", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], omitKeys: ["e_grid_out_day", "e_grid_in_day"] });
    const c = glanceCard(await GE.generateDashboard({ mode: "glance" }, hass));
    expect(c.totals.export_today).toBeUndefined();
    expect(c.totals.import_today).toBeUndefined();
    expect(c.totals.pv_today).toBeTruthy();
    expect(hasNullEntity(c)).toBe(false);
  });

  it("falls back to the classic EMS view set (no Glance panel) for an EMS plant", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({ mode: "glance" }, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "EMS Controls", "Diagnostics"]);
    expect(view(dash, "Glance")).toBeUndefined();
  });
});

describe("all mode", () => {
  it("leads with Glance then Flow then Analyst then the full classic view set", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], acCoupled: true });
    const dash = await GE.generateDashboard({ mode: "all" }, hass);
    expect(titles(dash)).toEqual([
      "Glance",
      "Flow",
      "Analyst",
      "Overview",
      "Energy",
      "Batteries",
      "Battery Health",
      "Controls",
      "Diagnostics",
    ]);
    expect(view(dash, "Glance").cards[0].type).toBe("custom:givenergy-glance");
    expect(view(dash, "Flow").cards[0].type).toBe("custom:givenergy-flow");
    expect(view(dash, "Analyst").cards[0].type).toBe("custom:givenergy-analyst");
  });

  it("falls back to the classic EMS view set for an EMS plant", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({ mode: "all" }, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "EMS Controls", "Diagnostics"]);
  });
});

describe("analyst mode", () => {
  const analystCard = (dash) => view(dash, "Analyst").cards[0];

  it("leads with a non-panel Analyst view with givenergy-analyst, apexcharts placeholder, and heatmaps", async () => {
    const hass = makeHass({ batterySerials: ["BAT1", "BAT2"], acCoupled: true });
    const dash = await GE.generateDashboard({ mode: "analyst" }, hass);
    expect(titles(dash)).toEqual([
      "Analyst",
      "Overview",
      "Energy",
      "Batteries",
      "Battery Health",
      "Controls",
      "Diagnostics",
    ]);
    const av = view(dash, "Analyst");
    expect(av.panel).toBeUndefined(); // non-panel
    expect(av.cards[0].type).toBe("custom:givenergy-analyst");
    // apexcharts not registered -> placeholder
    expect(av.cards[1].type).toBe("markdown");
    expect(av.cards[1].content).toContain("apexcharts-card");
    // one heatmap per battery pack
    expect(av.cards[2].type).toBe("custom:ge-cell-heatmap");
    expect(av.cards[3].type).toBe("custom:ge-cell-heatmap");
  });

  it("resolves all analyst entity slots from the registry and survives the loft_ prefix", async () => {
    const hass = makeHass({ batterySerials: ["BAT1"], areaPrefix: "loft_" });
    const dash = await GE.generateDashboard({ mode: "analyst" }, hass);
    const c = analystCard(dash);
    const registry = await regSet(hass);

    const liveSlots = [c.solar, c.grid, c.load, c.battery_power, c.battery_soc]
      .concat(c.solar_strings || [])
      .filter(Boolean);
    const totalSlots = Object.values(c.totals || {});
    const diagSlots  = Object.values(c.diag   || {});
    const allSlots   = liveSlots.concat(totalSlots).concat(diagSlots);

    expect(liveSlots.length).toBeGreaterThan(4);
    expect(totalSlots.length).toBe(6); // all 6 energy totals
    expect(diagSlots.length).toBeGreaterThan(4);

    for (const eid of allSlots) {
      expect(registry.has(eid)).toBe(true);
      expect(eid).toContain("loft_");
    }
    expect(hasNullEntity(c)).toBe(false);
  });

  it("omits missing totals and diag entities gracefully rather than emitting null", async () => {
    const hass = makeHass({
      batterySerials: ["BAT1"],
      omitKeys: ["e_grid_out_day", "e_grid_in_day", "consecutive_failures"],
    });
    const c = analystCard(await GE.generateDashboard({ mode: "analyst" }, hass));
    expect(c.totals.export_today).toBeUndefined();
    expect(c.totals.import_today).toBeUndefined();
    expect(c.totals.pv_today).toBeTruthy();
    expect(c.diag.consecutive_failures).toBeUndefined();
    expect(c.diag.t_inverter_heatsink).toBeTruthy();
    expect(hasNullEntity(c)).toBe(false);
  });

  it("falls back to the classic EMS view set (no Analyst view) for an EMS plant", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({ mode: "analyst" }, hass);
    expect(titles(dash)).toEqual(["Overview", "Energy", "EMS Controls", "Diagnostics"]);
    expect(view(dash, "Analyst")).toBeUndefined();
  });
});
