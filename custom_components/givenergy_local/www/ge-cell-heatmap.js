// GivEnergy cell-balance heatmap card (bundled with the givenergy_local
// integration and auto-registered as a frontend module - no manual install).
//
// Renders one row per battery pack: each of the 16 cell voltages coloured by
// its mV deviation from that pack's own mean (so imbalance is visible at any
// charge level), plus the pack mean (V) and spread (max-min, mV).
//
// Config:
//   type: custom:ge-cell-heatmap
//   batteries: [<serial>, ...]   # required
//   cells: 16                    # optional, default 16
//   span_mv: 15                  # optional colour-scale half-range, default 15
//   title: "..."                 # optional card header
//
// NOTE: text is emitted as HTML entities (not raw Unicode) on purpose - the
// inline-resource serving path mangles multibyte UTF-8, and ASCII-only source
// is immune to that class of bug.
class GeCellHeatmap extends HTMLElement {
  setConfig(cfg) {
    if (!cfg || !Array.isArray(cfg.batteries) || !cfg.batteries.length)
      throw new Error("ge-cell-heatmap: 'batteries: [serial, ...]' is required");
    this._cfg = cfg;
  }
  set hass(hass) { this._hass = hass; this._render(); }
  getCardSize() { return (this._cfg && this._cfg.batteries.length || 1) + 1; }

  _render() {
    const cfg = this._cfg, hass = this._hass;
    if (!hass) return;
    const nCells = cfg.cells || 16;
    // `set hass` fires on every HA state change, not just ours; skip the DOM
    // rebuild unless one of our cells (or the config) actually changed.
    const sig =
      (cfg.batteries || [])
        .map((s) => {
          const lo = s.toLowerCase();
          let cells = "";
          for (let n = 1; n <= nCells; n++) {
            const st = hass.states[`sensor.givenergy_battery_${lo}_cell_${n}_voltage`];
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
      const st = hass.states[`sensor.givenergy_battery_${s.toLowerCase()}_cell_${n}_voltage`];
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
}
if (!customElements.get("ge-cell-heatmap")) {
  customElements.define("ge-cell-heatmap", GeCellHeatmap);
}
