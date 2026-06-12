// Unit tests for the tape card's pure helpers (window arithmetic, rate
// parsing, event detection, SOC projection). Requires ge-tape.js without any
// browser stubs, so only the Node-exported API is evaluated - same discipline
// as ge-strategy.test.js.

const path = require("path");

const TAPE = require(
  path.join(__dirname, "..", "..", "custom_components", "givenergy_local", "www", "ge-tape.js")
);

const H = 3600 * 1000;
const NOW = Date.UTC(2026, 5, 12, 14, 0, 0); // 2026-06-12T14:00Z

describe("tape window", () => {
  it("spans -12h to +12h around now", () => {
    const win = TAPE.tapeWindow(NOW);
    expect(win.startMs).toBe(NOW - 12 * H);
    expect(win.endMs).toBe(NOW + 12 * H);
  });

  it("maps time to x linearly across the width", () => {
    const win = TAPE.tapeWindow(NOW);
    expect(TAPE.timeToX(win.startMs, win, 1000)).toBe(0);
    expect(TAPE.timeToX(NOW, win, 1000)).toBe(500);
    expect(TAPE.timeToX(win.endMs, win, 1000)).toBe(1000);
  });
});

describe("downsample", () => {
  it("returns short series unchanged", () => {
    const pts = [[1, 10], [2, 20]];
    expect(TAPE.downsample(pts, 10)).toEqual(pts);
  });

  it("strides long series but always keeps the last point", () => {
    const pts = [];
    for (let i = 0; i < 100; i++) pts.push([i, i]);
    const out = TAPE.downsample(pts, 10);
    expect(out.length).toBeLessThanOrEqual(11);
    expect(out[0]).toEqual([0, 0]);
    expect(out[out.length - 1]).toEqual([99, 99]);
  });
});

describe("parseForwardRates", () => {
  it("parses the Octopus rates attribute shape (pounds)", () => {
    const rates = TAPE.parseForwardRates({
      rates: [
        { start: "2026-06-12T14:00:00Z", end: "2026-06-12T14:30:00Z", value_inc_vat: 0.30 },
        { start: "2026-06-12T14:30:00Z", end: "2026-06-12T15:00:00Z", value_inc_vat: 0.07 },
      ],
    });
    expect(rates.length).toBe(2);
    expect(rates[0].startMs).toBe(Date.UTC(2026, 5, 12, 14, 0, 0));
    expect(rates[0].endMs).toBe(Date.UTC(2026, 5, 12, 14, 30, 0));
    expect(rates[0].rate).toBeCloseTo(0.30);
    expect(rates[1].rate).toBeCloseTo(0.07);
  });

  it("normalises pence-valued rates to pounds", () => {
    const rates = TAPE.parseForwardRates({
      rates: [{ start: "2026-06-12T14:00:00Z", end: "2026-06-12T14:30:00Z", value_inc_vat: 30 }],
    });
    expect(rates[0].rate).toBeCloseTo(0.30);
  });

  it("accepts valid_from/valid_to and value keys, and sorts by start", () => {
    const rates = TAPE.parseForwardRates({
      rates: [
        { valid_from: "2026-06-12T15:00:00Z", valid_to: "2026-06-12T15:30:00Z", value: 0.40 },
        { valid_from: "2026-06-12T14:00:00Z", valid_to: "2026-06-12T14:30:00Z", value: 0.20 },
      ],
    });
    expect(rates[0].rate).toBeCloseTo(0.20);
    expect(rates[1].rate).toBeCloseTo(0.40);
  });

  it("returns [] for missing or malformed attributes", () => {
    expect(TAPE.parseForwardRates(null)).toEqual([]);
    expect(TAPE.parseForwardRates({})).toEqual([]);
    expect(TAPE.parseForwardRates({ rates: "nope" })).toEqual([]);
  });
});

describe("classifyRates", () => {
  it("buckets cheap/standard/peak relative to the median", () => {
    const bands = TAPE.classifyRates([
      { startMs: 0, endMs: 1, rate: 0.07 },
      { startMs: 1, endMs: 2, rate: 0.30 },
      { startMs: 2, endMs: 3, rate: 0.55 },
    ]);
    expect(bands.map((b) => b.band)).toEqual(["cheap", "standard", "peak"]);
  });
});

describe("detectEvents", () => {
  it("marks export start/stop transitions with hysteresis", () => {
    const grid = [
      [1000, 0],
      [2000, 30],    // below the 50W floor: not an export start
      [3000, 400],   // export began
      [4000, 600],
      [5000, 10],    // export stopped
    ];
    const events = TAPE.detectEvents({ grid: grid });
    expect(events).toEqual([
      { tMs: 3000, kind: "export_started" },
      { tMs: 5000, kind: "export_stopped" },
    ]);
  });

  it("marks the battery reaching full", () => {
    const soc = [
      [1000, 97],
      [2000, 99],
      [3000, 100],
      [4000, 100], // stays full: only one event
    ];
    const events = TAPE.detectEvents({ soc: soc });
    expect(events).toEqual([{ tMs: 3000, kind: "soc_full" }]);
  });

  it("returns [] for empty input", () => {
    expect(TAPE.detectEvents({})).toEqual([]);
  });
});

describe("projectSoc", () => {
  const base = {
    startMs: NOW,
    horizonMs: 4 * H,
    stepMs: H,
    soc0: 50,
    capacityKwh: 10,
    chargeKw: 2.5,
    chargeSlots: [],
    dischargeSlots: [],
    pvKwAt: () => 0,
    loadKwAt: () => 0,
  };

  it("holds steady with no flows", () => {
    const pts = TAPE.projectSoc(base);
    expect(pts[0]).toEqual([NOW, 50]);
    expect(pts[pts.length - 1][1]).toBe(50);
  });

  it("ramps up inside a charge slot and clamps at 100", () => {
    const pts = TAPE.projectSoc({
      ...base,
      soc0: 80,
      chargeSlots: [{ startMs: NOW, endMs: NOW + 4 * H }],
    });
    // 2.5 kW into 10 kWh = +25%/h: 80 + 25 clamps to 100 after the first step.
    expect(pts[1][1]).toBeCloseTo(100);
    expect(pts[pts.length - 1][1]).toBe(100);
  });

  it("drains under net load and clamps at 0", () => {
    const pts = TAPE.projectSoc({
      ...base,
      soc0: 30,
      loadKwAt: () => 2.0, // -20%/h on 10 kWh
    });
    expect(pts[1][1]).toBeCloseTo(10);
    expect(pts[2][1]).toBeCloseTo(0);
    expect(pts[pts.length - 1][1]).toBe(0);
  });

  it("rises under PV surplus", () => {
    const pts = TAPE.projectSoc({
      ...base,
      pvKwAt: () => 3.0,
      loadKwAt: () => 1.0, // +20%/h
    });
    expect(pts[1][1]).toBeCloseTo(70);
  });
});
