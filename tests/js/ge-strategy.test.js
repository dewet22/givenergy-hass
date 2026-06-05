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
  it("emits the EMS view set and resolves the plant switch", async () => {
    const hass = makeHass({ ems: true });
    const dash = await GE.generateDashboard({}, hass);
    expect(titles(dash)).toEqual(["EMS Controls", "Diagnostics"]);

    const plant = card(view(dash, "EMS Controls"), byTitle("Plant"));
    const refs = collectRefs(plant);
    expect(refs.some((r) => r.endsWith("ems_plant_enable"))).toBe(true);
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
