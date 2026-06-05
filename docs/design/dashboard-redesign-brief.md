# Dashboard redesign · research brief

A companion to `dashboard-redesign-exploration.html`. Distils what the major
home-energy systems do, where they fall short, and what the five mockup
directions borrow from each.

## What every system gets right

The convergent design across Tesla, GivEnergy, Victron VRM, SolarEdge, Fronius
Solar.web, Enphase, and Sense is striking:

- **Animated power-flow diagram as the centrepiece.** Tesla's app, GivEnergy
  portal, Victron's "moving ants", SolarEdge's real-time flow illustrations,
  Sense's bubble view — all converge on the same idiom. People intuit a Sankey
  before they intuit a chart.
- **Big numbers up top.** SOC %, today's PV kWh, today's £/$. Always.
- **Multi-timescale switcher.** Day / week / month / year / lifetime.
- **Per-component drill-down.** Tesla's Energy graphs split by component (Home,
  Solar, Powerwall, Vehicle) — each gets its own page.
- **Real-time updates.** Victron pushes every 2 s when the user is on the page.
- **Self-consumption / autarky %.** The single most cited "is my system working
  for me?" metric.

## Where they differ (and what each one is best at)

- **Tesla** — story and impact framing. "Impact cards" show offsets and value
  rather than raw kWh. The narrative voice is unusual in the category.
- **GivEnergy** — portal/app split. Daily app, deep portal. Smart Tariff
  integration is the recent direction (auto-optimise battery against tariff).
- **Victron VRM** — schematic + history together on one page, system-adaptive
  layout, real-time toggleable. Most "engineer-friendly" of the consumer apps.
- **Enphase Enlighten** — per-panel diagnostics. Each panel's contribution
  visualised on the array layout; failure localised to a module.
- **SolarEdge mySolarEdge** — array layout view with module-level performance,
  plus Weather Guard (auto-charge before forecasted storm).
- **Sense** — appliance-level disaggregation via ML. Bubble of "what's on right
  now", with a per-device drilldown nobody else has.
- **HA community** — `power-flow-card-plus` and `tesla-style-solar-power-card`
  carry the visual idiom into HA; ApexCharts handles everything that needs more
  resolution. The official Energy dashboard standardises the data model but
  enforces a fixed visual structure.

## Where everything is thin (the opportunity space)

1. **Actionable "what should I do?" framing.** Almost every system answers
   *what's happening*; almost none answers *what to do next*. Tariff-aware
   recommendations ("run the dishwasher now, you're exporting at 15p") are
   the obvious missing piece — and now genuinely tractable because Octopus
   Agile/Flux APIs are HA-integrated.
2. **Narrative / story of the day.** The day rendered as a timeline with
   annotated events. Tesla gestures at this; nobody really commits to it.
3. **Calm / ambient.** Most dashboards assume engagement. Almost none assume
   the user just wants a glance-and-go: "is anything wrong, what's the number".
4. **Battery health & longevity.** Cycle counts, cell balance trends,
   degradation curves — surprisingly thin everywhere. GivEnergy exposes the
   sensors but no consumer dashboard makes them legible.
5. **Forecast-conditioned planning.** Victron has a solar forecast block;
   SolarEdge has Weather Guard; nobody chains "forecast → battery plan →
   appliance schedule → EV schedule" into one view.

## The five directions

The mockup picks five distinct points in this space rather than one consensus
design. They're meant to be compared, not ranked — and the ultimate strategy
should probably let the user choose (or compose) a primary direction.

| # | Direction | Best at | Borrows from | Risks |
|---|-----------|---------|--------------|-------|
| 01 | **Flow** | At-a-glance "what's happening now" | Tesla, GivEnergy, Victron, Sense | Becomes wallpaper; doesn't reveal much beyond the obvious |
| 02 | **Story** | Daily reflection / understanding patterns | NYT graphics, Tesla impact cards | Slow; one-shot per day; not for live monitoring |
| 03 | **Glance** | Mobile lock-screen / e-ink / ambient | Apple Home, calm-tech | Too thin if anything's actually wrong |
| 04 | **Analyst** | Diagnostics, debugging, optimisation | Grafana, Victron Advanced Dashboard | Hostile to non-engineers; overwhelming default |
| 05 | **Coach** | Decision support, tariff optimisation | Tesla impact cards, Octopus Agile-aware automations | Requires forecasting + tariff data; risk of nagging |

The five aren't mutually exclusive: a single dashboard could pin Glance at the
top, with Flow as the second-screen panel and Coach surfacing only when an
opportunity is detected. Analyst hides behind a "deep" affordance. Story could
be a daily digest email rather than a tab.

## What this means for the strategy

The strategy work goes from "reproduce the existing dashboard + sprinkle
distribution cards" to something stronger:

- **Strategy modes as a top-level option:** `strategy: { type: custom:givenergy,
  mode: glance | flow | story | analyst | coach | classic }`. `classic` reproduces
  the existing six-tab dashboard for parity.
- **Composable view strategies:** each mode is also exposable as a view strategy
  so users can drop, say, Glance as their entry view and Analyst as a second
  tab on the same dashboard.
- **Mode-specific entity needs:** Coach mode genuinely depends on directional
  power sensors (the signed-flow finding from earlier) plus a tariff feed and
  a solar forecast entity; Glance and Flow work today; Story needs the
  daily-energy split sensors that already exist.
- **Adaptive defaults:** the strategy can detect what's available and pick a
  sensible default mode — Coach if Octopus + forecast present, Flow otherwise,
  Glance on small viewports.

## How to iterate from here

The mockup is a single HTML file with all five directions side-by-side. Open it
in a browser and click through the five tabs. The data is real (today's actual
numbers from the loft inverter and both batteries) so the comparisons are
honest — what works for *your* data, not a hypothetical install.

Useful iteration moves:

- **Kill a direction.** If Story feels wrong, say so — the brief shrinks to four.
- **Steal across directions.** If Coach's tariff strip belongs in Flow, move it.
- **Push one direction further.** "Glance is right but should be even more
  minimal" or "Analyst needs the cell heatmap to be the centrepiece" are the
  cheapest, most valuable kinds of feedback.
- **Resize the window.** The mockup is responsive; see how each direction
  collapses to mobile. Several directions assume a lot of width.
