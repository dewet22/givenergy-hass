// GivEnergy tape module (bundled with the givenergy_local integration and
// auto-registered as a frontend module alongside ge-strategy.js).
//
// Defines the two cards behind `mode: mission`:
//   custom:givenergy-tape    - the day tape: a rolling -12h -> +12h timeline
//                              with "now" pinned centre. Layers: tariff bands,
//                              solar actual -> forecast, house consumption,
//                              SOC actual -> projection, charge/discharge plan
//                              blocks, event diamonds, and a now-cursor with a
//                              docked mini power flow. `variant: full` adds
//                              layer notes sized for a full-page panel.
//   custom:givenergy-mission - the Mission Control hub: glance strip on top,
//                              the tape as its spine, and a bottom tile row
//                              (ledger summary, health summary, next-action
//                              hint) linking to the deep tabs.
//
// Degradation rule: each missing feed (tariff entities, solar forecast, a
// failed history fetch) drops its layer and adds a one-line legend note - it
// never fails the card.
//
// NOTE: ASCII-only source on purpose - the /givenergy_local/ static serving
// path mangles multibyte UTF-8 (same lesson as ge-strategy.js).

(function () {
  "use strict";

  var H_MS = 3600 * 1000;
  var EXPORT_FLOOR_W = 50; // hysteresis floor for export start/stop events

  // ----- pure helpers (Node-exported for vitest) -----------------------------

  // Rolling window: -12h -> +12h around `now`, "now" pinned centre.
  function tapeWindow(nowMs) {
    return { startMs: nowMs - 12 * H_MS, endMs: nowMs + 12 * H_MS };
  }

  function timeToX(tMs, win, width) {
    return ((tMs - win.startMs) / (win.endMs - win.startMs)) * width;
  }

  // Stride-decimate a [[t, v], ...] series to at most ~maxN points, always
  // keeping the final point so the line reaches the cursor.
  function downsample(points, maxN) {
    if (!Array.isArray(points) || points.length <= maxN) return points;
    var stride = Math.ceil(points.length / maxN);
    var out = [];
    for (var i = 0; i < points.length; i += stride) out.push(points[i]);
    if (out[out.length - 1] !== points[points.length - 1]) {
      out.push(points[points.length - 1]);
    }
    return out;
  }

  // Normalise a tariff entity's forward-rates attribute to
  // [{startMs, endMs, rate}] in major currency units (pounds), sorted by
  // start. Accepts the shapes the Octopus Energy integration has shipped:
  // entries keyed start/end | valid_from/valid_to | from/to with
  // value_inc_vat | rate | value, in either pounds or pence (a rate above
  // 2.50 "pounds"/kWh is implausible, so values there are read as pence).
  function parseForwardRates(attrs) {
    if (!attrs || !Array.isArray(attrs.rates)) return [];
    var out = [];
    for (var i = 0; i < attrs.rates.length; i++) {
      var e = attrs.rates[i] || {};
      var start = e.start || e.valid_from || e.from;
      var end = e.end || e.valid_to || e.to;
      var raw = e.value_inc_vat != null ? e.value_inc_vat : e.rate != null ? e.rate : e.value;
      var startMs = Date.parse(start);
      var endMs = Date.parse(end);
      var rate = parseFloat(raw);
      if (isNaN(startMs) || isNaN(endMs) || isNaN(rate)) continue;
      if (rate > 2.5) rate /= 100;
      out.push({ startMs: startMs, endMs: endMs, rate: rate });
    }
    out.sort(function (a, b) {
      return a.startMs - b.startMs;
    });
    return out;
  }

  // Bucket each rate band relative to the median: <= 0.75x -> cheap,
  // >= 1.5x -> peak, else standard.
  function classifyRates(rates) {
    if (!rates.length) return [];
    var sorted = rates
      .map(function (r) {
        return r.rate;
      })
      .sort(function (a, b) {
        return a - b;
      });
    var mid = Math.floor(sorted.length / 2);
    var median =
      sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    return rates.map(function (r) {
      var band =
        r.rate <= 0.75 * median ? "cheap" : r.rate >= 1.5 * median ? "peak" : "standard";
      return { startMs: r.startMs, endMs: r.endMs, rate: r.rate, band: band };
    });
  }

  // Event diamonds from already-fetched history. `series` carries optional
  // [[tMs, value], ...] arrays: grid (W, positive = export) and soc (%).
  function detectEvents(series) {
    var events = [];
    var grid = (series && series.grid) || [];
    if (grid.length) {
      var exporting = grid[0][1] > EXPORT_FLOOR_W;
      for (var i = 1; i < grid.length; i++) {
        var w = grid[i][1];
        if (w > EXPORT_FLOOR_W && !exporting) {
          events.push({ tMs: grid[i][0], kind: "export_started" });
          exporting = true;
        } else if (w <= EXPORT_FLOOR_W && exporting) {
          events.push({ tMs: grid[i][0], kind: "export_stopped" });
          exporting = false;
        }
      }
    }
    var soc = (series && series.soc) || [];
    if (soc.length) {
      var full = soc[0][1] >= 100;
      for (var j = 1; j < soc.length; j++) {
        var pct = soc[j][1];
        if (pct >= 100 && !full) {
          events.push({ tMs: soc[j][0], kind: "soc_full" });
          full = true;
        } else if (pct < 100) {
          full = false;
        }
      }
    }
    events.sort(function (a, b) {
      return a.tMs - b.tMs;
    });
    return events;
  }

  // Honest-but-simple forward SOC projection: inside a charge (discharge)
  // slot the battery moves at +/- chargeKw; otherwise it absorbs the PV /
  // load balance. Clamped to [0, 100]. Returns [[tMs, soc], ...] including
  // the starting point.
  function projectSoc(o) {
    function inAny(slots, t) {
      for (var i = 0; i < slots.length; i++) {
        if (t >= slots[i].startMs && t < slots[i].endMs) return true;
      }
      return false;
    }
    var pts = [[o.startMs, o.soc0]];
    var soc = o.soc0;
    for (var t = o.startMs; t + o.stepMs <= o.startMs + o.horizonMs; t += o.stepMs) {
      var netKw;
      if (inAny(o.chargeSlots || [], t)) netKw = o.chargeKw;
      else if (inAny(o.dischargeSlots || [], t)) netKw = -o.chargeKw;
      else netKw = (o.pvKwAt ? o.pvKwAt(t) : 0) - (o.loadKwAt ? o.loadKwAt(t) : 0);
      soc += ((netKw * (o.stepMs / H_MS)) / o.capacityKwh) * 100;
      soc = Math.max(0, Math.min(100, soc));
      pts.push([t + o.stepMs, soc]);
    }
    return pts;
  }

  // "HH:MM[:SS]" (a time-entity state) -> minutes past local midnight, or null.
  function parseTimeOfDay(state) {
    if (typeof state !== "string") return null;
    var m = state.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
    if (!m) return null;
    return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  }

  // Project a daily [startMin, endMin) time-of-day slot onto every concrete
  // occurrence overlapping the window. Slots wrapping midnight split into the
  // evening and morning halves naturally via the day-by-day sweep.
  function slotOccurrences(startMin, endMin, win) {
    if (startMin == null || endMin == null) return [];
    if (startMin === endMin) return []; // 00:00-00:00 convention: disabled
    var out = [];
    var day = new Date(win.startMs);
    day.setHours(0, 0, 0, 0);
    for (var d = 0; d < 3; d++) {
      var midnight = day.getTime() + d * 24 * H_MS;
      var s = midnight + startMin * 60000;
      var e = midnight + endMin * 60000;
      if (endMin < startMin) e += 24 * H_MS; // wraps past midnight
      if (e > win.startMs && s < win.endMs) out.push({ startMs: s, endMs: e });
    }
    return out;
  }

  var API = {
    tapeWindow: tapeWindow,
    timeToX: timeToX,
    downsample: downsample,
    parseForwardRates: parseForwardRates,
    classifyRates: classifyRates,
    detectEvents: detectEvents,
    projectSoc: projectSoc,
    parseTimeOfDay: parseTimeOfDay,
    slotOccurrences: slotOccurrences,
  };

  // ----- browser-only from here ----------------------------------------------

  if (typeof customElements === "undefined") {
    if (typeof module !== "undefined" && module.exports) module.exports = API;
    return;
  }

  var W = 1200; // SVG viewBox width
  var TAPE_H = 360; // SVG viewBox height (tape area)
  var REFRESH_MS = 5 * 60 * 1000; // history refetch cadence
  var COLOURS = {
    solar: "#ffd33d",
    solarForecast: "#b08820",
    load: "#8b949e",
    soc: "#7ee787",
    socPlan: "#2ea043",
    charge: "#7ee787",
    discharge: "#79c0ff",
    cheap: "rgba(46,160,67,0.12)",
    peak: "rgba(248,81,73,0.14)",
    now: "#f85149",
    text: "#8b949e",
    axis: "#484f58",
  };

  function num(hass, eid) {
    if (!eid || !hass.states[eid]) return null;
    var v = parseFloat(hass.states[eid].state);
    return isNaN(v) ? null : v;
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // history/history_during_period -> [[tMs, value], ...] for one entity.
  function fetchSeries(hass, eid, startMs, endMs) {
    return hass
      .callWS({
        type: "history/history_during_period",
        start_time: new Date(startMs).toISOString(),
        end_time: new Date(endMs).toISOString(),
        entity_ids: [eid],
        minimal_response: true,
        no_attributes: true,
      })
      .then(function (res) {
        var rows = (res && res[eid]) || [];
        var out = [];
        for (var i = 0; i < rows.length; i++) {
          var v = parseFloat(rows[i].s);
          var t = rows[i].lu ? rows[i].lu * 1000 : Date.parse(rows[i].last_updated);
          if (!isNaN(v) && !isNaN(t)) out.push([t, v]);
        }
        return out;
      });
  }

  // Trailing 7-day mean consumption (W) per local hour-of-day from LTS, for
  // the SOC projection's load baseline.
  function fetchHourlyLoadBaseline(hass, eid, nowMs) {
    return hass
      .callWS({
        type: "recorder/statistics_during_period",
        start_time: new Date(nowMs - 7 * 24 * H_MS).toISOString(),
        end_time: new Date(nowMs).toISOString(),
        statistic_ids: [eid],
        period: "hour",
        types: ["mean"],
      })
      .then(function (res) {
        var rows = (res && res[eid]) || [];
        var sums = new Array(24).fill(0);
        var counts = new Array(24).fill(0);
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].mean == null) continue;
          var hour = new Date(rows[i].start).getHours();
          sums[hour] += rows[i].mean;
          counts[hour]++;
        }
        return sums.map(function (s, h) {
          return counts[h] ? s / counts[h] : null;
        });
      });
  }

  function pathFrom(points, win, yFn) {
    var d = "";
    for (var i = 0; i < points.length; i++) {
      var x = timeToX(points[i][0], win, W).toFixed(1);
      var y = yFn(points[i][1]).toFixed(1);
      d += (i ? "L" : "M") + x + "," + y;
    }
    return d;
  }

  var EVENT_LABELS = {
    export_started: "export began",
    export_stopped: "export ended",
    soc_full: "100% SOC",
  };

  // ----- custom:givenergy-tape ------------------------------------------------

  if (!customElements.get("givenergy-tape")) {
    customElements.define(
      "givenergy-tape",
      class GivEnergyTape extends HTMLElement {
        setConfig(cfg) {
          if (!cfg) throw new Error("givenergy-tape: config required");
          this._cfg = cfg;
          this._data = null;
          this._fetchedAt = 0;
          this._fetching = false;
        }

        set hass(hass) {
          this._hass = hass;
          this._maybeFetch();
          this._render();
        }

        getCardSize() {
          return this._cfg && this._cfg.variant === "full" ? 8 : 5;
        }

        _maybeFetch() {
          var self = this;
          if (this._fetching || Date.now() - this._fetchedAt < REFRESH_MS) return;
          this._fetching = true;
          var hass = this._hass;
          var cfg = this._cfg;
          var now = Date.now();
          var win = tapeWindow(now);
          var notes = [];

          function series(eid) {
            if (!eid) return Promise.resolve(null);
            return fetchSeries(hass, eid, win.startMs, now).catch(function () {
              return null;
            });
          }

          Promise.all([
            series(cfg.solar),
            series(cfg.load),
            series(cfg.grid),
            series(cfg.battery_soc),
            cfg.tariff_import
              ? series(cfg.tariff_import)
              : Promise.resolve(null),
            cfg.load
              ? fetchHourlyLoadBaseline(hass, cfg.load, now).catch(function () {
                  return null;
                })
              : Promise.resolve(null),
          ]).then(function (res) {
            self._data = {
              fetchedAt: now,
              solar: res[0],
              load: res[1],
              grid: res[2],
              soc: res[3],
              rateHistory: res[4],
              loadBaselineW: res[5],
              notes: notes,
            };
            self._fetchedAt = now;
            self._fetching = false;
            self._render();
          });
        }

        _slots(kind) {
          // kind: "charge" | "discharge"; config carries time-entity ids.
          var hass = this._hass;
          var win = tapeWindow(Date.now());
          var defs = this._cfg[kind + "_slots"] || [];
          var out = [];
          for (var i = 0; i < defs.length; i++) {
            var s = hass.states[defs[i].start];
            var e = hass.states[defs[i].end];
            if (!s || !e) continue;
            var occ = slotOccurrences(
              parseTimeOfDay(s.state),
              parseTimeOfDay(e.state),
              win
            );
            for (var j = 0; j < occ.length; j++) out.push(occ[j]);
          }
          return out;
        }

        _render() {
          var hass = this._hass;
          var cfg = this._cfg;
          if (!hass || !cfg) return;
          var now = Date.now();
          var win = tapeWindow(now);
          var data = this._data || {};
          var notes = [];

          // -- scales
          var kwTop = 60; // px band reserved for events row
          var kwBottom = TAPE_H - 24; // above the time axis
          var kwSpan = kwBottom - kwTop;
          var maxKw = (cfg.max_power_kw || 10) * 1000;
          var yPower = function (w) {
            var frac = Math.max(0, Math.min(1, w / maxKw));
            return kwBottom - frac * kwSpan;
          };
          var ySoc = function (pct) {
            return kwBottom - (Math.max(0, Math.min(100, pct)) / 100) * kwSpan;
          };

          var svg = [];

          // -- tariff bands (history behind, forward rates ahead)
          var rateState = cfg.tariff_import && hass.states[cfg.tariff_import];
          if (rateState) {
            var bands = classifyRates(parseForwardRates(rateState.attributes));
            for (var b = 0; b < bands.length; b++) {
              if (bands[b].band === "standard") continue;
              var bx0 = Math.max(0, timeToX(bands[b].startMs, win, W));
              var bx1 = Math.min(W, timeToX(bands[b].endMs, win, W));
              if (bx1 <= 0 || bx0 >= W || bx1 <= bx0) continue;
              svg.push(
                '<rect x="' + bx0.toFixed(1) + '" y="0" width="' + (bx1 - bx0).toFixed(1) +
                '" height="' + TAPE_H + '" fill="' + COLOURS[bands[b].band] + '"/>'
              );
            }
          } else if (cfg.tariff_import) {
            notes.push("tariff entity unavailable - bands hidden");
          } else {
            notes.push("no tariff_import configured - bands hidden");
          }

          // -- past area layers: load behind solar
          if (data.load && data.load.length) {
            var loadPts = downsample(data.load, 300);
            svg.push(
              '<path d="' + pathFrom(loadPts, win, yPower) +
              '" fill="none" stroke="' + COLOURS.load + '" stroke-width="1" opacity="0.7"/>'
            );
          }
          if (data.solar && data.solar.length) {
            var solarPts = downsample(data.solar, 300);
            var d0 = pathFrom(solarPts, win, yPower);
            var x0 = timeToX(solarPts[0][0], win, W).toFixed(1);
            var x1 = timeToX(solarPts[solarPts.length - 1][0], win, W).toFixed(1);
            svg.push(
              '<path d="' + d0 + " L" + x1 + "," + kwBottom + " L" + x0 + "," + kwBottom +
              ' Z" fill="' + COLOURS.solar + '" opacity="0.18" stroke="none"/>'
            );
            svg.push(
              '<path d="' + d0 + '" fill="none" stroke="' + COLOURS.solar + '" stroke-width="1.5"/>'
            );
          }

          // -- solar forecast (dashed, future)
          var fc = cfg.solar_forecast && hass.states[cfg.solar_forecast];
          var fcPts = [];
          if (fc && fc.attributes) {
            // Solcast-style detailed forecast: [{period_start, pv_estimate (kW)}].
            var det =
              fc.attributes.detailedForecast || fc.attributes.detailed_forecast || [];
            for (var f = 0; f < det.length; f++) {
              var ft = Date.parse(det[f].period_start || det[f].period_end);
              var fv = parseFloat(det[f].pv_estimate);
              if (!isNaN(ft) && !isNaN(fv) && ft >= now && ft <= win.endMs) {
                fcPts.push([ft, fv * 1000]);
              }
            }
            if (fcPts.length) {
              svg.push(
                '<path d="' + pathFrom(fcPts, win, yPower) + '" fill="none" stroke="' +
                COLOURS.solarForecast + '" stroke-width="1.5" stroke-dasharray="5,4"/>'
              );
            }
          } else if (cfg.solar_forecast) {
            notes.push("solar forecast unavailable");
          } else {
            notes.push("no solar_forecast configured");
          }

          // -- SOC: actual line + forward projection
          if (data.soc && data.soc.length) {
            svg.push(
              '<path d="' + pathFrom(downsample(data.soc, 300), win, ySoc) +
              '" fill="none" stroke="' + COLOURS.soc + '" stroke-width="2"/>'
            );
          }
          var socNow = num(hass, cfg.battery_soc);
          var capacity = num(hass, cfg.battery_capacity);
          if (socNow != null && capacity) {
            var baseline = data.loadBaselineW;
            var fcAt = function (t) {
              for (var i = 1; i < fcPts.length; i++) {
                if (fcPts[i][0] >= t) return fcPts[i - 1][1] / 1000;
              }
              return 0;
            };
            var loadAt = function (t) {
              if (!baseline) return 0.3; // modest default draw
              var w = baseline[new Date(t).getHours()];
              return w == null ? 0.3 : w / 1000;
            };
            var proj = projectSoc({
              startMs: now,
              horizonMs: win.endMs - now,
              stepMs: 15 * 60 * 1000,
              soc0: socNow,
              capacityKwh: capacity,
              chargeKw: (cfg.max_charge_kw || 2.5),
              chargeSlots: this._slots("charge"),
              dischargeSlots: this._slots("discharge"),
              pvKwAt: fcAt,
              loadKwAt: loadAt,
            });
            svg.push(
              '<path d="' + pathFrom(proj, win, ySoc) + '" fill="none" stroke="' +
              COLOURS.socPlan + '" stroke-width="1.5" stroke-dasharray="4,4"/>'
            );
          }

          // -- plan blocks (charge/discharge slot occurrences, future half only)
          var kinds = [
            ["charge", COLOURS.charge],
            ["discharge", COLOURS.discharge],
          ];
          for (var k = 0; k < kinds.length; k++) {
            var occs = this._slots(kinds[k][0]);
            for (var o = 0; o < occs.length; o++) {
              var px0 = Math.max(timeToX(occs[o].startMs, win, W), timeToX(now, win, W));
              var px1 = Math.min(W, timeToX(occs[o].endMs, win, W));
              if (px1 <= px0) continue;
              svg.push(
                '<rect x="' + px0.toFixed(1) + '" y="' + (kwBottom - 30) + '" width="' +
                (px1 - px0).toFixed(1) + '" height="24" rx="3" fill="none" stroke="' +
                kinds[k][1] + '" stroke-width="1"/>' +
                '<text x="' + (px0 + 5).toFixed(1) + '" y="' + (kwBottom - 13) +
                '" style="fill:' + kinds[k][1] + ';font-size:11px">' + kinds[k][0] + "</text>"
              );
            }
          }

          // -- event diamonds
          var events = detectEvents({ grid: data.grid, soc: data.soc });
          for (var ev = 0; ev < events.length; ev++) {
            var exx = timeToX(events[ev].tMs, win, W);
            if (exx < 0 || exx > W) continue;
            svg.push(
              '<text x="' + exx.toFixed(1) + '" y="30" text-anchor="middle" style="fill:' +
              COLOURS.text + ';font-size:11px">&#9670; ' +
              esc(EVENT_LABELS[events[ev].kind] || events[ev].kind) + "</text>"
            );
          }

          // -- time axis: a label every 3h
          var firstHour = new Date(win.startMs);
          firstHour.setMinutes(0, 0, 0);
          for (var t = firstHour.getTime(); t <= win.endMs; t += H_MS) {
            var hh = new Date(t).getHours();
            if (hh % 3 !== 0) continue;
            var ax = timeToX(t, win, W);
            if (ax < 12 || ax > W - 12) continue;
            svg.push(
              '<text x="' + ax.toFixed(1) + '" y="' + (TAPE_H - 8) +
              '" text-anchor="middle" style="fill:' + COLOURS.axis + ';font-size:11px">' +
              (hh < 10 ? "0" : "") + hh + ":00</text>"
            );
          }

          // -- now cursor + docked mini-flow
          var nx = timeToX(now, win, W);
          svg.push(
            '<line x1="' + nx + '" y1="0" x2="' + nx + '" y2="' + TAPE_H +
            '" stroke="' + COLOURS.now + '" stroke-width="2"/>'
          );
          var pv = num(hass, cfg.solar);
          var load = num(hass, cfg.load);
          var grid = num(hass, cfg.grid);
          var batt = num(hass, cfg.battery_power);
          var rate = rateState ? parseFloat(rateState.state) : null;
          var lines = [];
          if (pv != null) lines.push("solar " + (pv / 1000).toFixed(1) + " kW");
          if (load != null) lines.push("house " + (load / 1000).toFixed(1) + " kW");
          if (batt != null) {
            lines.push(
              "battery " + (batt >= 0 ? "+" : "") + (batt / 1000).toFixed(1) + " kW"
            );
          }
          if (grid != null) {
            lines.push(
              (grid >= 0 ? "export " : "import ") + Math.abs(grid / 1000).toFixed(1) + " kW" +
              (rate != null && !isNaN(rate)
                ? " @ " + (rate > 2.5 ? rate.toFixed(0) + "p" : (rate * 100).toFixed(0) + "p")
                : "")
            );
          }
          var boxW = 168;
          var boxX = Math.min(W - boxW - 4, nx + 8);
          var boxY = 50;
          svg.push(
            '<rect x="' + boxX + '" y="' + boxY + '" width="' + boxW + '" height="' +
            (18 + lines.length * 16) + '" rx="4" fill="#1f242c" stroke="' + COLOURS.now +
            '" stroke-width="1" opacity="0.95"/>'
          );
          svg.push(
            '<text x="' + (boxX + 8) + '" y="' + (boxY + 14) + '" style="fill:' + COLOURS.now +
            ';font-size:10px">NOW</text>'
          );
          for (var ln = 0; ln < lines.length; ln++) {
            svg.push(
              '<text x="' + (boxX + 8) + '" y="' + (boxY + 30 + ln * 16) +
              '" style="fill:#e6edf3;font-size:11px">' + esc(lines[ln]) + "</text>"
            );
          }

          // -- legend notes (full variant only; the strip stays clean)
          if (cfg.variant === "full" && notes.length) {
            for (var n = 0; n < notes.length; n++) {
              svg.push(
                '<text x="8" y="' + (16 + n * 14) + '" style="fill:' + COLOURS.axis +
                ';font-size:10px">' + esc(notes[n]) + "</text>"
              );
            }
          }

          this.innerHTML =
            '<ha-card style="background:#161a20;overflow:hidden">' +
            '<svg viewBox="0 0 ' + W + " " + TAPE_H +
            '" style="display:block;width:100%" preserveAspectRatio="xMidYMid meet">' +
            svg.join("") +
            "</svg></ha-card>";
        }
      }
    );
  }

  // ----- custom:givenergy-mission ----------------------------------------------

  if (!customElements.get("givenergy-mission")) {
    customElements.define(
      "givenergy-mission",
      class GivEnergyMission extends HTMLElement {
        setConfig(cfg) {
          if (!cfg) throw new Error("givenergy-mission: config required");
          this._cfg = cfg;
          this._tape = null;
        }

        set hass(hass) {
          this._hass = hass;
          this._render();
          if (this._tape) this._tape.hass = hass;
        }

        getCardSize() {
          return 8;
        }

        _glanceStrip() {
          var hass = this._hass;
          var cfg = this._cfg;
          var cells = [];

          var soc = num(hass, cfg.battery_soc);
          if (soc != null) cells.push(["SOC", soc.toFixed(0) + "%", "#7ee787"]);
          var pv = num(hass, cfg.solar);
          var pvDay = cfg.totals && num(hass, cfg.totals.pv_today);
          if (pv != null) {
            cells.push([
              "PV now" + (pvDay != null ? " - " + pvDay.toFixed(1) + " kWh today" : ""),
              (pv / 1000).toFixed(1) + " kW",
              "#ffd33d",
            ]);
          }
          var net = cfg.money && num(hass, cfg.money.net);
          if (net != null) {
            cells.push([
              "net cost today",
              (net < 0 ? "-" : "") + "GBP " + Math.abs(net).toFixed(2),
              "#79c0ff",
            ]);
          }
          var house = cfg.totals && num(hass, cfg.totals.house_today);
          if (house != null) cells.push(["house today", house.toFixed(1) + " kWh", "#8b949e"]);

          var html = '<div style="display:flex;gap:8px;margin-bottom:8px">';
          for (var i = 0; i < cells.length; i++) {
            html +=
              '<div style="flex:1;background:#1f242c;border-radius:6px;padding:10px;text-align:center">' +
              '<div style="color:' + cells[i][2] + ';font-size:22px;font-weight:600">' +
              esc(cells[i][1]) + "</div>" +
              '<div style="color:#8b949e;font-size:11px;margin-top:2px">' + esc(cells[i][0]) +
              "</div></div>";
          }
          return html + "</div>";
        }

        _tiles() {
          var hass = this._hass;
          var cfg = this._cfg;
          var tiles = [];

          if (cfg.money) {
            var net = num(hass, cfg.money.net);
            var cf = cfg.money.counterfactual && hass.states[cfg.money.counterfactual];
            var saved =
              cf && cf.attributes ? parseFloat(cf.attributes.savings_today) : NaN;
            var body =
              net != null
                ? "net GBP " + net.toFixed(2) +
                  (!isNaN(saved) ? " - saved GBP " + saved.toFixed(2) : "")
                : "awaiting data";
            tiles.push(["LEDGER", body, "ledger"]);
          }

          var packs = cfg.packs || [];
          var socs = [];
          for (var p = 0; p < packs.length; p++) {
            var v = num(hass, packs[p].soc);
            if (v != null) socs.push(packs[p].name + " " + v.toFixed(0) + "%");
          }
          tiles.push([
            "OBSERVATORY",
            socs.length ? socs.join(" / ") : "battery detail",
            "observatory",
          ]);

          // Next-action hint: current rate + next band change (heuristics only).
          var hint = "";
          var rs = cfg.tariff_import && hass.states[cfg.tariff_import];
          if (rs) {
            var rate = parseFloat(rs.state);
            if (!isNaN(rate)) {
              var pence = rate > 2.5 ? rate : rate * 100;
              hint = "import " + pence.toFixed(0) + "p/kWh now";
              var bands = classifyRates(parseForwardRates(rs.attributes));
              for (var b = 0; b < bands.length; b++) {
                if (bands[b].startMs > Date.now()) {
                  hint +=
                    " - " + bands[b].band + " from " +
                    new Date(bands[b].startMs).toTimeString().slice(0, 5);
                  break;
                }
              }
            }
          }
          tiles.push(["NEXT", hint || "no tariff data", "tape"]);

          var html = '<div style="display:flex;gap:8px;margin-top:8px">';
          for (var i = 0; i < tiles.length; i++) {
            html +=
              '<a href="' + tiles[i][2] +
              '" style="flex:1;background:#1f242c;border-radius:6px;padding:10px;text-decoration:none">' +
              '<span style="color:#8b949e;font-size:10px;letter-spacing:1px">' +
              esc(tiles[i][0]) + "</span> " +
              '<span style="color:#e6edf3;font-size:12px">' + esc(tiles[i][1]) + "</span></a>";
          }
          return html + "</div>";
        }

        _render() {
          var hass = this._hass;
          if (!hass || !this._cfg) return;
          if (!this._tape) {
            this.innerHTML =
              '<div style="padding:8px;background:#11141a;min-height:100%">' +
              '<div id="ge-mission-strip"></div>' +
              '<div id="ge-mission-tape"></div>' +
              '<div id="ge-mission-tiles"></div></div>';
            this._tape = document.createElement("givenergy-tape");
            var tapeCfg = {};
            for (var k in this._cfg) tapeCfg[k] = this._cfg[k];
            tapeCfg.type = "custom:givenergy-tape";
            tapeCfg.variant = "strip";
            this._tape.setConfig(tapeCfg);
            this.querySelector("#ge-mission-tape").appendChild(this._tape);
          }
          this.querySelector("#ge-mission-strip").innerHTML = this._glanceStrip();
          this.querySelector("#ge-mission-tiles").innerHTML = this._tiles();
        }
      }
    );
  }

  // Node (vitest): export the pure helpers for unit testing.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
})();
