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
  const M = 60000;

  it("needs >250W to start an export, <50W to stop it", () => {
    const grid = [
      [0, 0],
      [5 * M, 150],   // above the stop floor but below the start threshold
      [10 * M, 400],  // export began
      [40 * M, 400],
      [45 * M, 10],   // export stopped
    ];
    const events = TAPE.detectEvents({ grid: grid });
    expect(events).toEqual([
      { tMs: 10 * M, kind: "export_started" },
      { tMs: 45 * M, kind: "export_stopped" },
    ]);
  });

  it("drops export episodes shorter than the debounce window", () => {
    const grid = [
      [0, 0],
      [10 * M, 400], // 3-minute blip: meter noise, not an export session
      [13 * M, 0],
      [60 * M, 0],
    ];
    expect(TAPE.detectEvents({ grid: grid })).toEqual([]);
  });

  it("bridges short dips inside a long export episode", () => {
    const grid = [
      [0, 300],       // already exporting at the window edge: no start event
      [20 * M, 0],    // 2-minute dip...
      [22 * M, 300],  // ...resumes: bridged, no stop/start pair
      [40 * M, 300],
      [41 * M, 0],    // the real stop
    ];
    expect(TAPE.detectEvents({ grid: grid })).toEqual([
      { tMs: 41 * M, kind: "export_stopped" },
    ]);
  });

  it("keeps a trailing open episode even when younger than the window", () => {
    const grid = [
      [0, 0],
      [55 * M, 400], // export began 5 minutes ago and is still running
      [60 * M, 400],
    ];
    expect(TAPE.detectEvents({ grid: grid })).toEqual([
      { tMs: 55 * M, kind: "export_started" },
    ]);
  });

  it("marks the battery reaching full once, re-arming only below 95%", () => {
    const soc = [
      [1 * M, 97],
      [2 * M, 99],
      [3 * M, 100],
      [4 * M, 100],  // stays full: only one event
      [5 * M, 99],   // shallow dip: not re-armed
      [6 * M, 100],  // no second event
    ];
    const events = TAPE.detectEvents({ soc: soc });
    expect(events).toEqual([{ tMs: 3 * M, kind: "soc_full" }]);
  });

  it("returns [] for empty input", () => {
    expect(TAPE.detectEvents({})).toEqual([]);
  });
});

describe("flowLines", () => {
  it("formats the live flow summary, pricing the grid leg", () => {
    expect(
      TAPE.flowLines({ pvW: 800, loadW: 700, battW: 0, gridW: -100, ratePence: 30 })
    ).toEqual(["solar 0.8 kW", "house 0.7 kW", "battery +0.0 kW", "import 0.1 kW @ 30p"]);
  });

  it("labels an exporting grid leg and omits missing slots", () => {
    expect(TAPE.flowLines({ gridW: 1500, ratePence: 15 })).toEqual([
      "export 1.5 kW @ 15p",
    ]);
    expect(TAPE.flowLines({ pvW: 2000 })).toEqual(["solar 2.0 kW"]);
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

describe("bandSplit", () => {
  // A cumulative day-counter (kWh) sampled across two tariff bands: deltas
  // are allocated to the band in force at the sample time.
  const bands = [
    { startMs: 0, endMs: 10000, rate: 0.07, band: "cheap" },
    { startMs: 10000, endMs: 20000, rate: 0.55, band: "peak" },
  ];

  it("allocates counter deltas to the band at each sample", () => {
    const series = [
      [2000, 1.0],
      [8000, 3.0],  // +2.0 in cheap
      [12000, 3.5], // +0.5 in peak
      [18000, 5.0], // +1.5 in peak
    ];
    const split = TAPE.bandSplit(series, bands);
    expect(split.cheap).toBeCloseTo(2.0);
    expect(split.peak).toBeCloseTo(2.0);
    expect(split.standard).toBeCloseTo(0);
  });

  it("ignores midnight resets and samples outside any band", () => {
    const series = [
      [2000, 5.0],
      [8000, 0.2],  // counter reset: negative delta ignored
      [9000, 1.2],  // +1.0 in cheap
      [25000, 2.2], // +1.0 outside all bands -> standard
    ];
    const split = TAPE.bandSplit(series, bands);
    expect(split.cheap).toBeCloseTo(1.0);
    expect(split.standard).toBeCloseTo(1.0);
    expect(split.peak).toBeCloseTo(0);
  });

  it("handles empty inputs", () => {
    expect(TAPE.bandSplit([], bands)).toEqual({ cheap: 0, standard: 0, peak: 0 });
    expect(TAPE.bandSplit(null, [])).toEqual({ cheap: 0, standard: 0, peak: 0 });
  });
});

describe("sumChange", () => {
  it("totals LTS change rows, skipping nulls", () => {
    expect(
      TAPE.sumChange([{ change: 1.5 }, { change: null }, { change: -0.5 }, {}])
    ).toBeCloseTo(1.0);
  });

  it("returns null when no rows carry a change", () => {
    expect(TAPE.sumChange([])).toBeNull();
    expect(TAPE.sumChange([{ change: null }])).toBeNull();
  });
});

describe("nextActionHint", () => {
  const rates = {
    rates: [
      { start: "2026-06-12T14:00:00Z", end: "2026-06-12T16:00:00Z", value_inc_vat: 0.15 },
      { start: "2026-06-12T16:00:00Z", end: "2026-06-12T19:00:00Z", value_inc_vat: 0.55 },
      { start: "2026-06-12T19:00:00Z", end: "2026-06-12T23:00:00Z", value_inc_vat: 0.15 },
    ],
  };

  it("shows the import rate and the next band change when importing", () => {
    const hint = TAPE.nextActionHint({
      nowMs: NOW, // 14:00Z
      gridW: -800, // importing
      importState: { state: "0.15", attributes: rates },
      exportState: null,
    });
    expect(hint).toContain("import 15p/kWh");
    expect(hint).toContain("peak");
  });

  it("shows the export rate while exporting", () => {
    const hint = TAPE.nextActionHint({
      nowMs: NOW,
      gridW: 1200, // exporting
      importState: { state: "0.15", attributes: rates },
      exportState: { state: "0.18", attributes: {} },
    });
    expect(hint).toContain("exporting at 18p/kWh");
  });

  it("returns empty without tariff data", () => {
    expect(TAPE.nextActionHint({ nowMs: NOW, gridW: 0, importState: null })).toBe("");
  });
});

describe("collectRates", () => {
  // The Octopus integration's day-rates event entities: each carries a
  // `rates` attribute; previous/current/next day merge into one timeline.
  const evt = (day, rate) => ({
    attributes: {
      rates: [
        {
          start: `2026-06-${day}T00:00:00+01:00`,
          end: `2026-06-${day}T12:00:00+01:00`,
          value_inc_vat: rate,
        },
      ],
    },
  });

  it("merges rates from several state objects, sorted by start", () => {
    const merged = TAPE.collectRates([evt(13, 0.30), evt(11, 0.069), evt(12, 0.25)]);
    expect(merged.length).toBe(3);
    expect(merged.map((r) => r.rate)).toEqual([0.069, 0.25, 0.30]);
  });

  it("skips null states and dedupes identical band starts", () => {
    const merged = TAPE.collectRates([null, evt(12, 0.25), evt(12, 0.25)]);
    expect(merged.length).toBe(1);
  });

  it("returns [] for empty input", () => {
    expect(TAPE.collectRates([])).toEqual([]);
    expect(TAPE.collectRates(null)).toEqual([]);
  });
});

describe("nextActionHint with explicit forward rates", () => {
  it("prefers passed-in forward rates over the sensor's attributes", () => {
    const hint = TAPE.nextActionHint({
      nowMs: NOW, // 14:00Z
      gridW: -800,
      importState: { state: "0.30", attributes: {} }, // no rates here (new Octopus shape)
      forwardRates: TAPE.classifyRates([
        { startMs: NOW - H, endMs: NOW + 8 * H, rate: 0.30 },
        { startMs: NOW + 8 * H, endMs: NOW + 10 * H, rate: 0.069 },
        { startMs: NOW + 10 * H, endMs: NOW + 12 * H, rate: 0.30 },
      ]),
    });
    expect(hint).toContain("import 30p/kWh");
    expect(hint).toContain("cheap");
  });
});

describe("forecastPoints", () => {
  const fc = (hour, kw) => ({
    attributes: {
      detailedForecast: [
        { period_start: `2026-06-12T${hour}:00:00Z`, pv_estimate: kw },
      ],
    },
  });

  it("merges detailedForecast entries from several states, in watts, sorted", () => {
    const pts = TAPE.forecastPoints([fc("16", 2.5), fc("15", 3.0)], NOW, NOW + 12 * H);
    expect(pts).toEqual([
      [Date.UTC(2026, 5, 12, 15, 0, 0), 3000],
      [Date.UTC(2026, 5, 12, 16, 0, 0), 2500],
    ]);
  });

  it("clips to the window and skips null/malformed states", () => {
    const pts = TAPE.forecastPoints(
      [null, fc("10", 9.9), fc("15", 3.0)], // 10:00 is before the window
      NOW,
      NOW + 12 * H
    );
    expect(pts.length).toBe(1);
    expect(pts[0][1]).toBe(3000);
  });
});
