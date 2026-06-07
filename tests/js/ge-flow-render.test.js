// Render-level regression test for the custom:givenergy-flow element's SVG edge
// labels. The two edges crossing the diagram centre - solar->battery (vertical)
// and grid->home (horizontal) - share a bezier midpoint, so when both flows are
// active their kW labels used to print on top of each other as an unreadable
// run-together. The fix offsets each label perpendicular to its own axis
// (solar->battery labelDx:-38, grid->home labelDy:-26); this test pins that the
// two centre labels' bounding boxes stay clear of one another.
//
// Unlike ge-strategy.test.js (which only asserts the generated card *config*),
// this exercises the element's _render() and reads the rendered <text> geometry.
// The flow element is only defined when `customElements` exists, so we install a
// minimal HTMLElement/customElements stub BEFORE requiring the module - hence a
// separate file from ge-strategy.test.js, which deliberately requires it without
// one. The element writes its SVG via this.innerHTML, so the stub never needs a
// real DOM: we read the string back and parse the .e-label geometry out of it.

const path = require("path");

// --- minimal browser stubs, installed before the require below ----------------
global.HTMLElement = class HTMLElement {};
const _defs = {};
global.customElements = {
  define: (name, cls) => { if (!_defs[name]) _defs[name] = cls; },
  get: (name) => _defs[name],
  whenDefined: () => Promise.resolve(),
};

const { makeHass } = require("./mock-hass");
const GE = require(
  path.join(__dirname, "..", "..", "custom_components", "givenergy_local", "www", "ge-strategy.js")
);

// Edge colours from ge-strategy.js, used to pick out the two centre-crossing
// labels among all active edge labels (solar->home shares neither colour).
const CHARGE = "#4a9fd4"; // solar->battery (vertical centre edge)
const IMPORT = "#e5734d"; // grid->home    (horizontal centre edge)

// Approximate rendered label box, halo included: ~42px wide x ~13px tall, centred
// on the text anchor (text-anchor:middle, dominant-baseline:middle).
const LABEL_W = 42, LABEL_H = 13;

function boxesOverlap(a, b) {
  return Math.abs(a.x - b.x) < LABEL_W && Math.abs(a.y - b.y) < LABEL_H;
}

// Parse every <text class="e-label" ...> out of the rendered SVG string, keyed by
// fill colour. Attribute order is fixed by the template: x, y, then style.
function edgeLabels(html) {
  const re = /<text class="e-label" x="(-?[\d.]+)" y="(-?[\d.]+)" style="fill:([^"]+)">([^<]*)<\/text>/g;
  const out = {};
  let m;
  while ((m = re.exec(html)) !== null) {
    out[m[3]] = { x: parseFloat(m[1]), y: parseFloat(m[2]), text: m[4] };
  }
  return out;
}

// Build a flow card and render it against a hass.states snapshot where BOTH
// centre flows are simultaneously active: solar (3 kW) exceeds the battery charge
// demand (1 kW), and the grid imports (2 kW) to the home. That gives
// flowSolarToBatt = 1000 W and flowGridToHome = 2000 W, both > THRESH.
async function renderFlow(states) {
  const hass = makeHass({ batterySerials: ["BAT1"] });
  const dash = await GE.generateDashboard({ mode: "flow" }, hass);
  const cfg = dash.views.find((v) => v.title === "Flow").cards[0];

  const stateMap = {
    [cfg.solar]: { state: "3000" },         // 3 kW PV
    [cfg.grid]: { state: "-2000" },         // -2 kW => importing from grid
    [cfg.load]: { state: "2000" },          // 2 kW house draw
    [cfg.battery_power]: { state: "-1000" },// -1 kW => charging the battery
    [cfg.battery_soc]: { state: "75" },
  };

  const El = customElements.get("givenergy-flow");
  const el = new El();
  el.setConfig(cfg);
  el.hass = { states: states || stateMap };
  return el.innerHTML;
}

describe("givenergy-flow centre-label collision", () => {
  it("renders both centre-stream edge labels when both flows are active", async () => {
    const labels = edgeLabels(await renderFlow());
    expect(labels[CHARGE]).toBeTruthy(); // solar->battery label present
    expect(labels[IMPORT]).toBeTruthy(); // grid->home label present
    expect(labels[CHARGE].text).toBe("1.00 kW");
    expect(labels[IMPORT].text).toBe("2.00 kW");
  });

  it("keeps the solar->battery and grid->home labels from overlapping", async () => {
    const labels = edgeLabels(await renderFlow());
    // Guards the centre-label collision: with the perpendicular offsets the two
    // boxes are clear (grid->home lifted 26px above the shared midpoint); drop
    // grid->home's labelDy and they land on the same y and this fails.
    expect(boxesOverlap(labels[CHARGE], labels[IMPORT])).toBe(false);
  });
});
