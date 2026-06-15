#!/usr/bin/env python3
"""
migrate_from_givtcp.py — Migrate GivTCP long-term energy statistics to givenergy_local.

Uses the Home Assistant WebSocket API. Requires:
  - Home Assistant running and reachable
  - A Long-Lived Access Token:
      HA → Profile → Security → Long-Lived Access Tokens → Create token
  - Python 3.11+ with the `websockets` package:
      pip install "websockets>=12.0"

Dry-run is the default. Pass --apply to write changes.

Typical workflow:

  Step 1 — find your cut-over date (omit --cutover to auto-detect):

    python scripts/migrate_from_givtcp.py \\
        --ha-url http://homeassistant.local:8123 \\
        --token eyJhbGc...

  Step 2 — preview what will be migrated:

    python scripts/migrate_from_givtcp.py \\
        --ha-url http://homeassistant.local:8123 \\
        --token eyJhbGc... \\
        --cutover 2024-10-31

  Step 3 — apply (back up your recorder DB first):

    python scripts/migrate_from_givtcp.py \\
        --ha-url http://homeassistant.local:8123 \\
        --token eyJhbGc... \\
        --cutover 2024-10-31 \\
        --apply \\
        --max-kw 10

The cut-over date is the boundary between GivTCP and givenergy_local history.
GivTCP data strictly before midnight on that date is migrated; givenergy_local
data from midnight on that date onwards is kept. Any givenergy_local data before
that boundary is discarded — it will typically be partial or recorded in parallel
with GivTCP. Choosing a day when both integrations were running means the full
GivTCP history is captured and GE takes over from 00:00 that day regardless of
when GivTCP actually stopped.

Sum reconstruction (default): rather than copy GivTCP's `sum` column and rebase
it at the join, the script concatenates the `state` timeline across the cut-over
and walks it once to produce a single continuous `sum`. This removes the join
seam entirely. The walk is plausibility-guarded and reset-aware: a decrease in
`state` only counts as a legitimate counter reset at that counter's natural
boundary — `_today` sensors at local midnight, `_this_year` sensors at the year
boundary, `_total` sensors never. Off-boundary drops and over-ceiling spikes hold
the last good value instead of booking the bogus jump. The plausibility ceiling is
the user-declared --max-kw value (used directly as the authoritative bound under
--apply) with the adaptive per-entity p99 estimate as the fallback for dry-run
previews where no cap is given.

Pass --trust-source-sums to restore the legacy behaviour (copy GivTCP's sum
column verbatim and rebase at the join) for installs whose GivTCP sums are
known-good.

Serials are auto-detected — no hard-coding needed. Multi-inverter and
multi-battery setups are handled automatically.

givenergy_local target entities are resolved against the live entity/device
registry, so HA 2026.6 area prefixes (e.g. sensor.loft_givenergy_inverter_…)
and user renames are followed automatically. GivTCP source entities are read
as-is — if they were themselves renamed, the source side won't be found.

What is migrated by default:
  Solar generation today / lifetime
  Grid import today / lifetime
  Grid export today / lifetime
  Battery charge today / discharge today
  PV generation today / lifetime
  Battery throughput lifetime
  House consumption today  (GivTCP's load_energy_today_kwh → givenergy_local's
    derived house_consumption_today; givenergy-modbus #174. The old
    load_energy_today read ~0 and was excluded; the derived figure is correct.)

Opt-in (--include-charge-from-grid):
  Charge from grid lifetime  ⚠️  values differ on some systems — verify manually

Not migrated (no GivTCP equivalent, register-level gap, or incompatible type):
  battery_discharge_this_year, work_time_total, total_refresh_failures,
  battery_charge_energy_total_kwh, battery_discharge_energy_total_kwh,
  load_energy_total_kwh
  battery_cycles  (GivTCP records per-pack charge cycles as a *mean* statistic
    [state_class measurement], but givenergy_local's charge_cycles is a
    total_increasing *sum* series. The two are incompatible shapes — there is no
    sum column to rebase, and forcing it would corrupt the GE counter — so the
    pre-GE cycle history is not migrated. See BATTERY_PAIRS below.)

Post-migration validation (automatic, read-only, runs in both dry-run and apply):
  Re-reads each migrated sum series and flags residual implausible hours,
  fake-reset shapes, duplicate series, and gaps. Exits non-zero on substantive
  findings; gaps are reported informationally and do not affect the exit code.
  Pass --repair-residue (only meaningful with --apply) to clear and re-import the
  rebuilt series for sum entities that still show implausible hours.

See docs/migration-from-givtcp.md for the full sensor catalogue and design notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

# `websockets` is imported lazily in HAWebSocket.connect() so this module stays
# importable (for unit-testing the pure helpers below) without the dependency.


# ---------------------------------------------------------------------------
# Entity mapping
# ---------------------------------------------------------------------------

# (givtcp_suffix, ge_suffix, description, included_by_default)
# Suffixes are relative to the integration prefix + inverter serial, e.g.:
#   givtcp  : sensor.givtcp_<inv_sn>_<givtcp_suffix>
#   ge_local: sensor.givenergy_inverter_<inv_sn>_<ge_suffix>
INVERTER_PAIRS: list[tuple[str, str, str, bool]] = [
    ("pv_energy_today_kwh", "pv_energy_today", "Solar generation today", True),
    ("pv_energy_total_kwh", "pv_energy_total", "Solar generation lifetime", True),
    ("import_energy_today_kwh", "grid_import_today", "Grid import today", True),
    ("import_energy_total_kwh", "grid_import_total", "Grid import lifetime", True),
    ("export_energy_today_kwh", "grid_export_today", "Grid export today", True),
    ("export_energy_total_kwh", "grid_export_total", "Grid export lifetime", True),
    ("battery_charge_energy_today_kwh", "battery_charge_today", "Battery charge today", True),
    (
        "battery_discharge_energy_today_kwh",
        "battery_discharge_today",
        "Battery discharge today",
        True,
    ),
    # givenergy-modbus #174/#176: IR44/45-46 are PV generation, so these
    # sensors were renamed from "inverter output" to "PV generation".
    ("invertor_energy_today_kwh", "pv_generation_today", "PV generation today", True),
    ("invertor_energy_total_kwh", "pv_generation_total", "PV generation lifetime", True),
    (
        "battery_throughput_total_kwh",
        "battery_throughput_total",
        "Battery throughput lifetime",
        True,
    ),
    # House consumption: GivTCP's load_energy_today_kwh is its computed house
    # consumption. The old givenergy_local "load_energy_today" (e_load_day/IR35)
    # was a mislabel that read ~0, so this pair was excluded. givenergy-modbus
    # #174 added the real derived figure, surfaced as house_consumption_today —
    # the correct target. Both are PV + grid-in - grid-out - AC-charge, so they
    # align; validate the overlap before an --apply.
    ("load_energy_today_kwh", "house_consumption_today", "House consumption today", True),
    # ⚠️  Diverged — the two integrations read different register blocks for this
    # sensor, so live values disagree on some systems. Included only with
    # --include-charge-from-grid; verify the imported values manually afterwards.
    ("ac_charge_energy_total_kwh", "charge_from_grid_total", "Charge from grid lifetime", False),
]

# (givtcp_suffix, ge_suffix, description, fallback_unit)
# Suffixes relative to: sensor.givtcp_<batt_sn>_<givtcp_suffix>
#                  and: sensor.givenergy_battery_<batt_sn>_<ge_suffix>
#
# Intentionally empty. GivTCP DOES expose per-battery cycle counts
# (sensor.givtcp_<batt_sn>_battery_cycles), and givenergy_local has a matching
# charge_cycles sensor — but they are incompatible LTS shapes: GivTCP records
# cycles as a *mean* statistic (state_class measurement) while charge_cycles is
# total_increasing (a *sum* series). migrate_entity rebases the source's sum
# column onto the GE series; with no sum to read it would rebase GE to ~0 and
# corrupt the existing counter. A correct migration would need a bespoke
# mean→counter path; the lifetime cycle count is low-value as LTS, so the pair
# is omitted rather than mis-migrated. (Was added in #126, reverted here.)
BATTERY_PAIRS: list[tuple[str, str, str, str]] = []

# ---------------------------------------------------------------------------
# Serial detection
# ---------------------------------------------------------------------------

_SERIAL = r"[a-zA-Z]{2}\d{4}[a-zA-Z]\d+"

_INV_DETECT = re.compile(rf"^sensor\.givtcp_({_SERIAL})_pv_energy_today_kwh$", re.IGNORECASE)
_BATT_DETECT = re.compile(rf"^sensor\.givtcp_({_SERIAL})_battery_cycles$", re.IGNORECASE)

# Reference suffixes used for cut-over date detection
_CUTOVER_DETECT_GIVTCP = "pv_energy_today_kwh"
_CUTOVER_DETECT_GE = "pv_energy_today"


# ---------------------------------------------------------------------------
# Entity-id resolution
# ---------------------------------------------------------------------------
#
# The mappings above name givenergy_local targets in their canonical form,
# `{domain}.givenergy_{kind}_{serial}_{slug}`. HA 2026.6 prefixes generated
# entity_ids with the device area (`sensor.loft_givenergy_inverter_…`) and users
# can rename entities, so the canonical id may not be the real statistic_id. We
# rebuild the same canonical→actual map the dashboard generator uses (see
# custom_components.givenergy_local._build_entity_id_resolver), but from the
# WebSocket entity/device registries rather than the in-process ones.

_GE_PLATFORM = "givenergy_local"


def _slugify(text: str) -> str:
    """ASCII slugify matching homeassistant.util.slugify for GivEnergy names.

    The integration's device and sensor names are plain ASCII, so lowercasing
    and collapsing runs of non-alphanumerics to a single underscore reproduces
    HA's slug. A test pins this against homeassistant.util.slugify for the
    device names and every mapped sensor name.
    """
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def build_entity_id_resolver(
    entity_entries: list[dict[str, Any]],
    device_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Return a canonical→actual map for givenergy_local entity ids.

    Mirrors `_build_entity_id_resolver`: reconstruct each entity's canonical id
    from its device name and original (integration-assigned) name — the stable
    identity the entity_id is slugged from — and map it to the entity's actual
    id. Entries missing a device name or original name are skipped. Callers wrap
    the result with `.get(eid, eid)` so unknown ids pass through unchanged.
    """
    device_name_by_id: dict[str, str | None] = {}
    for dev in device_entries:
        device_id = dev.get("id") or dev.get("device_id")
        if device_id:
            device_name_by_id[device_id] = dev.get("name")

    canonical_to_actual: dict[str, str] = {}
    for ent in entity_entries:
        if ent.get("platform") != _GE_PLATFORM:
            continue
        device_name = device_name_by_id.get(ent.get("device_id"))
        original_name = ent.get("original_name")
        entity_id = ent.get("entity_id")
        if not device_name or not original_name or not entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        canonical = f"{domain}.{_slugify(device_name)}_{_slugify(original_name)}"
        canonical_to_actual[canonical] = entity_id
    return canonical_to_actual


# ---------------------------------------------------------------------------
# Recorder write resilience
# ---------------------------------------------------------------------------
#
# HA's recorder runs single-threaded. A large import_statistics call blocks that
# thread, so a following clear_statistics can wait past HA's internal command
# timeout and return a 'timeout' error — even though the queued delete still
# executes. That leaves an entity cleared but not re-imported. To avoid it:
#   - chunk imports so no single call monopolises the recorder thread;
#   - retry recorder mutations on a timeout (both clear and import are
#     idempotent — clear of an empty series is a no-op; import upserts by
#     (statistic_id, start) — so a retry after a timed-out-but-applied call is
#     safe);
#   - pace entities so the recorder can drain between them.
_IMPORT_CHUNK_ROWS = 1000
_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY = 2.0  # seconds; linear backoff: delay * attempt
_ENTITY_PAUSE_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Entity reset-class classification
# ---------------------------------------------------------------------------


class ResetClass(Enum):
    """How a counter is expected to reset, controlling reset-vs-corruption calls."""

    DAILY = "daily"  # _today sensors: reset to 0 at local midnight
    ANNUAL = "annual"  # _this_year sensors: reset at the year boundary
    LIFETIME = "lifetime"  # _total sensors: never reset within the migration window


def classify_entity(ge_suffix: str) -> ResetClass:
    """Classify a givenergy_local sensor suffix by its expected reset cadence."""
    if ge_suffix.endswith("_today"):
        return ResetClass.DAILY
    if ge_suffix.endswith("_this_year"):
        return ResetClass.ANNUAL
    return ResetClass.LIFETIME


# ---------------------------------------------------------------------------
# Adaptive plausibility ceiling
# ---------------------------------------------------------------------------

_CEILING_MIN_SAMPLES = 24
_CEILING_PERCENTILE = 99.0
_CEILING_FACTOR = 1.5

_REBASELINE_HOLDS = 3  # consecutive coherent holds before re-baselining
_FLAT_LINE_MIN_HOURS = 6  # min span for a flat-line finding
_MOVEMENT_TOLERANCE_PCT = 5.0  # source-vs-rebuilt movement divergence tolerance


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank (floor-index) percentile of an ascending-sorted, non-empty
    list. Uses the floor index rather than linear interpolation so a single
    order-of-magnitude outlier near the top cannot drag the percentile up toward
    itself on small samples (which would let the spike bless itself through the
    ceiling)."""
    return sorted_vals[int((len(sorted_vals) - 1) * p / 100.0)]


def adaptive_ceiling(deltas: list[float | None]) -> float | None:
    """Per-hour plausibility ceiling from an entity's positive state-deltas.

    Genuine hourly deltas are bounded by the hardware (inverter clip, battery
    subsystem rate); corruption sits orders of magnitude higher as a tiny (<1%)
    tail. The 99th percentile tracks the real physical limit without being pulled
    up by the rare fakes; the 1.5x factor adds headroom for legitimate peaks
    while still rejecting order-of-magnitude spikes. Returns None below
    _CEILING_MIN_SAMPLES (and on empty) so the caller fails closed rather than
    guessing a bound from too little data.
    """
    pos = sorted(d for d in deltas if d is not None and d > 0)
    if len(pos) < _CEILING_MIN_SAMPLES:
        return None
    return _percentile(pos, _CEILING_PERCENTILE) * _CEILING_FACTOR


def _apply_requires_cap(apply: bool, max_kw: float | None) -> bool:
    """True when an --apply run is missing the required --max-kw bound."""
    return apply and max_kw is None


def effective_ceiling(adaptive: float | None, cap: float | None) -> float | None:
    """The ceiling the rebuild/validation guards against.

    A user-declared cap (--max-kw) is the trusted bound and takes PRECEDENCE:
    deltas up to it are legitimate by the user's declaration, so the rebuild
    must not flatten them — even where the adaptive p99 estimate would sit lower.
    The adaptive estimate is only the fallback when no cap is given (the dry-run
    preview/report path; --apply requires --max-kw, so its rebuild always uses
    the user-declared bound)."""
    if cap is not None:
        return cap
    return adaptive


# ---------------------------------------------------------------------------
# Reset-boundary detection
# ---------------------------------------------------------------------------


def _is_reset_boundary(
    start_iso: str,
    reset_class: ResetClass,
    tz: ZoneInfo,
    tol_hours: float,
) -> bool:
    """True if a decrease at this timestamp is a legitimate counter reset.

    DAILY counters reset within ``tol_hours`` of local midnight; ANNUAL counters
    within ``tol_hours`` of local Jan-1 00:00; LIFETIME counters never reset.
    Evaluated in local time so DST (London resets at 23:00Z in summer, 00:00Z in
    winter) and inverter-clock lag are handled.
    """
    if reset_class is ResetClass.LIFETIME:
        return False
    local = datetime.fromisoformat(start_iso).astimezone(tz)
    tol_minutes = tol_hours * 60
    minutes_into_day = local.hour * 60 + local.minute
    dist_to_midnight = min(minutes_into_day, 24 * 60 - minutes_into_day)
    if reset_class is ResetClass.DAILY:
        return dist_to_midnight <= tol_minutes
    # ANNUAL: near midnight AND on Dec 31 / Jan 1.
    near_midnight = dist_to_midnight <= tol_minutes
    on_year_edge = (local.month, local.day) in {(1, 1), (12, 31)}
    return near_midnight and on_year_edge


# ---------------------------------------------------------------------------
# Sum-column rebuild walk
# ---------------------------------------------------------------------------


def _elapsed_hours(prev_start: str, start: str) -> float:
    """Whole-ish hours between two ISO timestamps, floored at 1 (so the per-hour
    bound applies to adjacent readings and scales up across a gap)."""
    delta = (_to_utc(start) - _to_utc(prev_start)).total_seconds() / 3600.0
    return max(1.0, delta)


def _gap_crosses_reset(prev_start: str, start: str, reset_class: ResetClass, tz: ZoneInfo) -> bool:
    """True if a natural reset boundary for *reset_class* falls STRICTLY within
    (prev_start, start), in local time. A reading exactly on the boundary is a
    normal reset row, not a crossed gap. LIFETIME never resets."""
    if reset_class is ResetClass.LIFETIME:
        return False
    a = _to_utc(prev_start).astimezone(tz)
    b = _to_utc(start).astimezone(tz)
    if b <= a:
        return False
    if reset_class is ResetClass.DAILY:
        first_midnight = (a + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return first_midnight < b  # strictly before b
    # ANNUAL: first Jan-1 boundary after a, strictly before b
    first_jan1 = a.replace(
        year=a.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return first_jan1 < b


def _segment_coherent(held: list[tuple[str, float]], ceiling: float, tz: ZoneInfo) -> bool:
    """True if the held (start, state) readings form a monotonic cumulative
    segment: every adjacent internal delta is in [0, ceiling × elapsed_pair].
    The offset from the prior baseline to held[0] is evaluated by the caller
    (it may be either direction)."""
    for (sa, va), (sb, vb) in zip(held, held[1:]):
        if not (0 <= vb - va <= ceiling * _elapsed_hours(sa, sb)):
            return False
    return True


def _smear_gap(
    prev_sum: float, total_delta: float, prev_start: str, end_start: str, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Daily-boundary rows climbing linearly from prev_sum across (prev_start,
    end_start). The caller emits the real end row (carrying prev_sum+total_delta);
    these fill the gap so each day shows a plausible share, not one spike."""
    start_dt = _to_utc(prev_start)
    end_dt = _to_utc(end_start)
    total = (end_dt - start_dt).total_seconds()
    if total <= 0:
        return []
    rows: list[dict[str, Any]] = []
    local = start_dt.astimezone(tz)
    day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        boundary = day.astimezone(UTC)
        if boundary >= end_dt:
            break
        frac = (boundary - start_dt).total_seconds() / total
        s = round(prev_sum + total_delta * frac, 6)
        rows.append({"start": _as_iso(boundary), "sum": s, "state": s})
        day += timedelta(days=1)
    return rows


def rebuild_sum_walk(
    rows: list[dict[str, Any]],
    reset_class: ResetClass,
    ceiling: float,
    tz: ZoneInfo,
    midnight_tol_hours: float = 2.0,
    events: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild a clean cumulative ``sum`` from ``state``; recover from sustained
    shifts instead of flat-lining. See the design spec for the full rationale.

    ``events`` (if given) accumulates lists under keys: ``rebaseline``, ``smear``,
    ``gap_undercount``, ``unresolved`` — surfaced by validation.
    """

    def _ev(key: str, payload: dict) -> None:
        if events is not None:
            events.setdefault(key, []).append(payload)

    out: list[dict[str, Any]] = []
    running = 0.0
    prev_state: float | None = None
    prev_start: str | None = None
    held: list[tuple[str, float]] = []  # (start, state) buffered, not yet emitted

    def _emit(start: str, sum_val: float, state_val: float | None) -> None:
        out.append(
            {
                "start": start,
                "sum": round(sum_val, 6),
                "state": None if state_val is None else round(state_val, 6),
            }
        )

    def _flush_transient() -> None:
        # The held run was a transient spike: emit each as last-good (flat).
        for s, _st in held:
            _emit(s, running, prev_state)
        held.clear()

    def _flush_segment() -> None:
        nonlocal running, prev_state, prev_start
        base = held[0][1]
        # offset (held[0] - prev_state) is suppressed; internal deltas are booked.
        for s, st in held:
            _emit(s, running + (st - base), running + (st - base))
        _ev(
            "rebaseline",
            {"start": held[0][0], "offset": round(base - prev_state, 3), "held": len(held)},
        )
        running += held[-1][1] - base
        prev_state = held[-1][1]
        prev_start = held[-1][0]
        held.clear()

    for row in rows:
        state = row.get("state")
        start = row["start"]
        if state is None:
            # gap row: carry running forward (do not resolve held on a None row).
            # Before any data is accepted (prev_state is None) there is no
            # last-good reading to carry, so emit a genuine None rather than
            # fabricating a 0.0 state that downstream reset/delta logic would
            # treat as real.
            _emit(start, running, prev_state)
            continue
        if prev_state is None:
            running = float(state)
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue

        elapsed = _elapsed_hours(prev_start, start)
        bound = ceiling * elapsed
        # (1) Reset-crossing gap FIRST, before any delta-sign branch.
        if elapsed > 1 and _gap_crosses_reset(prev_start, start, reset_class, tz):
            _flush_transient()
            _emit(start, running, running)  # carry flat across the gap
            _ev("gap_undercount", {"start": start, "from": prev_start})
            prev_state = float(state)
            prev_start = start
            continue

        delta = state - prev_state
        # (2) Accept genuine (possibly multi-hour) accumulation.
        if 0 <= delta <= bound:
            _flush_transient()
            if elapsed > 1:
                smeared = _smear_gap(running, delta, prev_start, start, tz)
                if smeared:
                    out.extend(smeared)
                    _ev(
                        "smear",
                        {"start": start, "energy": round(delta, 3), "hours": round(elapsed, 1)},
                    )
            running += delta
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue
        # (3) Boundary reset (intra-reading, no gap).
        if delta < 0 and _is_reset_boundary(start, reset_class, tz, midnight_tol_hours):
            _flush_transient()
            running += state
            prev_state = float(state)
            prev_start = start
            _emit(start, running, running)
            continue
        # (4) Otherwise: buffer as held; try to confirm a coherent segment.
        #     Coherence bounds each held pair by its OWN elapsed time (passes the
        #     (start, state) tuples + ceiling), not the single cumulative `bound`.
        held.append((start, float(state)))
        if len(held) >= _REBASELINE_HOLDS and _segment_coherent(held, ceiling, tz):
            _flush_segment()

    if held:
        _ev("unresolved", {"start": held[0][0], "count": len(held)})
        _flush_transient()  # emit last-good; the recorded event makes the gate refuse
    # Held/smeared rows can be appended after a later-timestamped gap row, so the
    # raw append order is not guaranteed sorted. Restore the ascending-by-start
    # contract that import + Phase-C verify_written rely on. Every emitted row
    # (real, held, smeared) carries an ISO start.
    out.sort(key=lambda r: _to_utc(r["start"]))
    return out


# ---------------------------------------------------------------------------
# Home Assistant WebSocket client
# ---------------------------------------------------------------------------


class HAWebSocket:
    """Minimal async HA WebSocket client covering only what the migration needs."""

    def __init__(self, base_url: str, token: str) -> None:
        ws_base = base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._url = f"{ws_base}/api/websocket"
        self._token = token
        self._ws: Any = None
        self._msg_id = 0

    async def connect(self) -> None:
        try:
            import websockets.asyncio.client
        except ImportError:
            sys.exit("Missing dependency: pip install 'websockets>=12.0'")
        # max_size=None lifts the default 1 MiB frame cap: the entity/device
        # registry listings (and large recorder responses) routinely exceed it
        # on a populated HA instance. This is a trusted, admin-token local tool.
        self._ws = await websockets.asyncio.client.connect(self._url, max_size=None)
        hello = await self._recv()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake message: {hello}")
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_result = await self._recv()
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(
                "Authentication failed — check your Long-Lived Access Token.\n"
                f"  HA response: {auth_result}"
            )

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()

    async def _recv(self) -> dict[str, Any]:
        return json.loads(await self._ws.recv())

    async def _call(self, msg_type: str, **kwargs: Any) -> Any:
        self._msg_id += 1
        msg_id = self._msg_id
        await self._ws.send(json.dumps({"type": msg_type, "id": msg_id, **kwargs}))
        while True:
            msg = await self._recv()
            if msg.get("id") == msg_id:
                if not msg.get("success", True):
                    raise RuntimeError(f"HA returned an error for '{msg_type}': {msg.get('error')}")
                return msg.get("result")

    async def _call_with_retry(self, msg_type: str, **kwargs: Any) -> Any:
        """Call a recorder mutation, retrying on HA's 'timeout' error.

        A recorder command that times out client-side may still be queued and
        applied server-side, so we only retry on timeout (not other errors) and
        rely on the operations being idempotent.
        """
        last_exc: RuntimeError | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return await self._call(msg_type, **kwargs)
            except RuntimeError as exc:
                if "timeout" not in str(exc).lower():
                    raise
                last_exc = exc
                if attempt < _RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
        assert last_exc is not None
        raise last_exc

    async def list_statistic_ids(self, statistic_type: str = "sum") -> list[dict[str, Any]]:
        result = await self._call("recorder/list_statistic_ids", statistic_type=statistic_type)
        return result or []

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        result = await self._call("config/entity_registry/list")
        return result or []

    async def list_device_registry(self) -> list[dict[str, Any]]:
        result = await self._call("config/device_registry/list")
        return result or []

    async def get_timezone(self) -> ZoneInfo:
        """Return the HA instance's configured local timezone (UTC fallback)."""
        cfg = await self._call("get_config")
        name = (cfg or {}).get("time_zone") or "UTC"
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("UTC")

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime | None = None,
        types: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        kwargs: dict[str, Any] = {
            "statistic_ids": statistic_ids,
            "start_time": start.isoformat(),
            "period": "hour",
            "types": types or ["sum", "state"],
        }
        if end is not None:
            kwargs["end_time"] = end.isoformat()
        result = await self._call("recorder/statistics_during_period", **kwargs)
        return result or {}

    async def clear_statistics(self, statistic_ids: list[str]) -> None:
        await self._call_with_retry("recorder/clear_statistics", statistic_ids=statistic_ids)

    async def import_statistics(
        self, metadata: dict[str, Any], stats: list[dict[str, Any]]
    ) -> None:
        # Chunk so no single import monopolises the recorder thread long enough
        # to time out a following command. Each chunk carries the same metadata;
        # import upserts by (statistic_id, start), so chunks accumulate.
        if len(stats) <= _IMPORT_CHUNK_ROWS:
            await self._call_with_retry(
                "recorder/import_statistics", metadata=metadata, stats=stats
            )
            return
        for i in range(0, len(stats), _IMPORT_CHUNK_ROWS):
            chunk = stats[i : i + _IMPORT_CHUNK_ROWS]
            await self._call_with_retry(
                "recorder/import_statistics", metadata=metadata, stats=chunk
            )


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _to_utc(ts: int | float | str) -> datetime:
    """Accept a Unix timestamp (int/float, s or ms) or ISO-8601 string; return UTC datetime."""
    if isinstance(ts, (int, float)):
        # HA returns millisecond timestamps in modern versions (values > 1e11).
        if ts > 1e11:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=UTC)
    return datetime.fromisoformat(str(ts)).astimezone(UTC)


def _as_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a statistics row with `start` as ISO UTC string and no `end`."""
    r = {k: v for k, v in row.items() if k != "end"}
    r["start"] = _as_iso(_to_utc(r["start"]))
    return r


# ---------------------------------------------------------------------------
# Validation checks (pure)
# ---------------------------------------------------------------------------


def find_implausible_hours(rows: list[dict[str, Any]], ceiling: float) -> list[dict[str, Any]]:
    """Rows whose sum-change from the previous row exceeds the ceiling."""
    flagged = []
    for prev, cur in zip(rows, rows[1:]):
        ps, cs = prev.get("sum"), cur.get("sum")
        if ps is None or cs is None:
            continue
        if cs - ps > ceiling:
            flagged.append({"start": cur["start"], "change": round(cs - ps, 3)})
    return flagged


_FLAT_EPSILON = 1e-6


def find_flat_line_spans(
    rows: list[dict[str, Any]], min_hours: int = _FLAT_LINE_MIN_HOURS
) -> list[dict[str, Any]]:
    """Maximal runs of consecutive equal-sum rows whose DURATION (from timestamps,
    not row count) is >= min_hours. Epsilon comparison handles float/synthetic
    sums. Returns {start, end, hours}; the caller exempts spans that overlap a
    recorded gap_undercount interval."""
    spans: list[dict[str, Any]] = []
    run_start = 0
    for i in range(1, len(rows) + 1):
        changed = (
            i == len(rows)
            or abs((rows[i].get("sum") or 0.0) - (rows[i - 1].get("sum") or 0.0)) > _FLAT_EPSILON
        )
        if not changed:
            continue
        last = i - 1
        if last > run_start:  # at least two rows in the run
            duration = (
                _to_utc(rows[last]["start"]) - _to_utc(rows[run_start]["start"])
            ).total_seconds() / 3600.0
            if duration >= min_hours:
                spans.append(
                    {
                        "start": rows[run_start]["start"],
                        "end": rows[last]["start"],
                        "hours": round(duration, 6),
                    }
                )
        run_start = i
    return spans


def _unexplained_flat_portions(
    span: dict[str, Any], covered_intervals: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Subtract the covered (gap_undercount) intervals from a flat *span* and
    return the residual contiguous pieces ``{start, end, hours}`` left unexplained.

    A short accepted reset gap that merely touches a multi-day flat does NOT
    exempt the whole span — only the overlapping slice is removed, so a residual
    portion can still be long enough to block.
    """
    span_a = _to_utc(span["start"])
    span_b = _to_utc(span["end"])
    if span_b <= span_a:
        return []
    # Clip each covered interval to the span, keep non-empty overlaps, merge.
    clipped: list[tuple[datetime, datetime]] = []
    for cs, ce in covered_intervals:
        lo = max(span_a, _to_utc(cs))
        hi = min(span_b, _to_utc(ce))
        if hi > lo:
            clipped.append((lo, hi))
    clipped.sort()
    merged: list[tuple[datetime, datetime]] = []
    for lo, hi in clipped:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    # Walk the gaps between the merged covered intervals.
    residual: list[dict[str, Any]] = []
    cursor = span_a
    for lo, hi in merged:
        if lo > cursor:
            residual.append(_flat_piece(cursor, lo))
        cursor = max(cursor, hi)
    if span_b > cursor:
        residual.append(_flat_piece(cursor, span_b))
    return residual


def _flat_piece(a: datetime, b: datetime) -> dict[str, Any]:
    return {
        "start": _as_iso(a),
        "end": _as_iso(b),
        "hours": round((b - a).total_seconds() / 3600.0, 6),
    }


def _reset_aware_movement(
    rows: list[dict[str, Any]], reset_class: ResetClass, tz: ZoneInfo
) -> float:
    """Genuine accumulation across *rows*: positive consecutive deltas, plus the
    post-reset state at a legitimate reset boundary (a reset drops the raw value,
    so max(0, Δ) would lose that day's pre-reset accumulation otherwise)."""
    total = 0.0
    prev = None
    for r in rows:
        st = r.get("state")
        if st is None:
            continue
        if prev is not None:
            if st < prev and _is_reset_boundary(r["start"], reset_class, tz, 2.0):
                total += st  # post-reset accumulation
            elif st >= prev:
                total += st - prev  # genuine forward movement
            # an off-boundary drop contributes nothing (corruption)
        prev = st
    return total


def compare_source_movement(
    source_movement: float,
    rebuilt_movement: float,
    upward_offsets: float,
    tol_pct: float = _MOVEMENT_TOLERANCE_PCT,
) -> dict[str, Any]:
    """Compare rebuilt movement to *cleaned expected* = source movement minus the
    recorded UPWARD rebase offsets the rebuild intentionally dropped. Downward
    offsets were never in the positive source movement, so they are NOT
    subtracted. Movements are reset-aware (see _reset_aware_movement) over the
    same aligned window."""
    expected = source_movement - upward_offsets
    denom = abs(expected) if abs(expected) > 1e-9 else 1.0
    diff_pct = abs(rebuilt_movement - expected) / denom * 100.0
    return {
        "expected": round(expected, 3),
        "rebuilt": round(rebuilt_movement, 3),
        "diff_pct": round(diff_pct, 2),
        "flagged": diff_pct > tol_pct,
    }


def _dedup_series(
    series_by_id: dict[str, list[dict[str, Any]]],
    units_by_id: dict[str, str | None],
) -> dict[str, list[dict[str, Any]]]:
    """Restrict duplicate detection to sum entities.

    ``find_duplicate_series`` keys on ``(start, sum)``.  Mean entities (power,
    SOC, temperature) carry no sum, so their rows come back with ``sum=None`` and
    two genuinely different mean series over the same hours collapse to identical
    ``(start, None)`` key tuples — a false-positive duplicate.  Only sum entities
    (those present in ``units_by_id`` — the same signal ``_repairable`` uses) are
    candidates for duplicate detection.
    """
    return {sid: rows for sid, rows in series_by_id.items() if sid in units_by_id}


def find_duplicate_series(series_by_id: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str]]:
    """Pairs of statistic ids whose (start, sum) sequences are byte-identical."""

    def key(rows: list[dict[str, Any]]) -> tuple:
        return tuple((r.get("start"), r.get("sum")) for r in rows)

    seen: dict[tuple, str] = {}
    dupes: list[tuple[str, str]] = []
    for sid, rows in series_by_id.items():
        k = key(rows)
        if k in seen:
            dupes.append((seen[k], sid))
        else:
            seen[k] = sid
    return dupes


def classify_gaps(
    rows: list[dict[str, Any]], expected_step_minutes: int = 60
) -> list[dict[str, Any]]:
    """Contiguous missing spans (more than one expected step between rows)."""
    gaps = []
    step = timedelta(minutes=expected_step_minutes)
    for prev, cur in zip(rows, rows[1:]):
        delta = _to_utc(cur["start"]) - _to_utc(prev["start"])
        missing = round(delta / step) - 1
        if missing >= 1:
            gaps.append({"after": prev["start"], "before": cur["start"], "hours": missing})
    return gaps


def find_fake_reset_shapes(rows: list[dict[str, Any]], ceiling: float) -> list[dict[str, Any]]:
    """A drop to ~0 immediately followed by a huge positive jump (modbus zero-read)."""
    shapes = []
    for i in range(2, len(rows)):
        a, b, c = rows[i - 2].get("state"), rows[i - 1].get("state"), rows[i].get("state")
        if a is None or b is None or c is None:
            continue
        if a > 0 and b <= a * 0.05 and (c - b) > ceiling:
            shapes.append({"start": rows[i]["start"], "recovery": round(c - b, 3)})
    return shapes


_REPORT_HEADERS = {
    "dry-run": "Validation report (dry-run: current series)",
    "candidates": "Validation report (candidates to write)",
    "post-migration": "Validation report (post-migration)",
}


def format_validation_report(
    findings: dict[str, dict[str, list]],
    duplicates: list[tuple[str, str]],
    mode: str = "dry-run",
) -> tuple[str, int]:
    """Render the validation findings; return (text, exit_code).

    exit_code is non-zero when substantive issues exist (implausible hours,
    fake-reset shapes, or duplicate series). Gaps are reported informationally
    and do not affect the exit code.

    ``mode`` selects the header and names what was validated:

    - ``"dry-run"`` — the in-memory candidate series the script would write,
      previewed without touching the recorder; findings reflect what migration
      *would* produce.
    - ``"candidates"`` — same in-memory candidates, validated under ``--apply``
      just before Phase B writes them (the apply gate reads this).
    - ``"post-migration"`` — series re-read from the recorder after a write.

    In every mode the findings are computed against the rebuilt candidate, never
    the pre-migration series.
    """
    header = _REPORT_HEADERS[mode]
    lines = ["", header, "─" * 72]
    substantive = False
    for ge_id, f in findings.items():
        impl = f.get("implausible", [])
        fakes = f.get("fake_resets", [])
        gaps = f.get("gaps", [])
        if not (impl or fakes or gaps):
            continue
        lines.append(f"  {ge_id}")
        for row in impl:
            substantive = True
            lines.append(f"    implausible +{row['change']} at {row['start']}")
        for row in fakes:
            substantive = True
            lines.append(f"    fake-reset shape (+{row['recovery']}) at {row['start']}")
        for g in gaps:
            lines.append(f"    gap {g['hours']}h: {g['after']} -> {g['before']}")
    for a, b in duplicates:
        substantive = True
        lines.append(f"  duplicate series: {a} == {b}")
    if not substantive and not any(f.get("gaps") for f in findings.values()):
        lines.append("  no issues found")
    lines.append("─" * 72)
    return "\n".join(lines), (1 if substantive else 0)


# ---------------------------------------------------------------------------
# Cut-over date detection
# ---------------------------------------------------------------------------


async def detect_cutover(
    ws: HAWebSocket,
    inv_sn: str,
    resolve: Callable[[str], str],
) -> tuple[date | None, date | None]:
    """
    Return (last_givtcp_date, first_ge_date) for the reference inverter sensor.

    Either value may be None if no data was found.
    """
    givtcp_id = f"sensor.givtcp_{inv_sn}_{_CUTOVER_DETECT_GIVTCP}"
    ge_id = resolve(f"sensor.givenergy_inverter_{inv_sn}_{_CUTOVER_DETECT_GE}")

    now = datetime.now(tz=UTC)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    # Scan the past 5 years for the last GivTCP data point; go back further if
    # the recorder has a longer history.
    lookback_start = now - timedelta(days=5 * 365)

    raw_givtcp = await ws.get_statistics([givtcp_id], lookback_start, end=now)
    raw_ge = await ws.get_statistics([ge_id], epoch, end=now)

    givtcp_rows = raw_givtcp.get(givtcp_id, [])
    ge_rows = raw_ge.get(ge_id, [])

    last_givtcp = _to_utc(givtcp_rows[-1]["start"]).date() if givtcp_rows else None
    first_ge = _to_utc(ge_rows[0]["start"]).date() if ge_rows else None

    return last_givtcp, first_ge


# ---------------------------------------------------------------------------
# Sum rebase
# ---------------------------------------------------------------------------


def rebase_sum(stats: list[dict[str, Any]], base_sum: float) -> list[dict[str, Any]]:
    """
    Shift the `sum` column so the first row starts at `base_sum`.

    The delta between successive rows is preserved, so the shape of the curve
    is unchanged — the series simply continues from where GivTCP left off.
    """
    if not stats:
        return []
    first_sum = stats[0].get("sum") or 0.0
    offset = base_sum - first_sum
    if abs(offset) < 1e-9:
        return [dict(r) for r in stats]
    result = []
    for row in stats:
        r = dict(row)
        if r.get("sum") is not None:
            r["sum"] = round(r["sum"] + offset, 6)
        result.append(r)
    return result


def build_merged_states(
    givtcp_rows: list[dict[str, Any]],
    ge_rows: list[dict[str, Any]],
    cutover: datetime,
) -> list[dict[str, Any]]:
    """Concatenate the state timeline across the cut-over for a single entity.

    GivTCP rows strictly before the cut-over, then givenergy_local rows from the
    cut-over onward, each carrying ``state``. Sorted ascending by ``start``. This
    is the input to ``rebuild_sum_walk`` — walking it produces one continuous
    sum, so the join seam never exists.
    """
    pre = [r for r in givtcp_rows if _to_utc(r["start"]) < cutover]
    post = [r for r in ge_rows if _to_utc(r["start"]) >= cutover]
    merged = pre + post
    merged.sort(key=lambda r: _to_utc(r["start"]))
    return merged


# ---------------------------------------------------------------------------
# Per-entity migration
# ---------------------------------------------------------------------------


class MigrationResult:
    __slots__ = (
        "description",
        "ge_id",
        "warn_diverged",
        "warn_no_ge_pre",
        "status",
        "givtcp_rows",
        "ge_pre_rows",
        "ge_post_rows",
        "merged_rows",
        "sum_at_cutover",
        "error",
        "rebuilt_rows",
        "events",
        "source_movement",
        "upward_offsets",
        "post_movement",
        "ge_post_movement",
        "metadata",
    )

    def __init__(self, description: str, ge_id: str, warn_diverged: bool = False) -> None:
        self.description = description
        self.ge_id = ge_id
        self.warn_diverged = warn_diverged
        self.warn_no_ge_pre = False
        self.status = "pending"
        self.givtcp_rows = 0
        self.ge_pre_rows = 0
        self.ge_post_rows = 0
        self.merged_rows = 0
        self.sum_at_cutover: float | None = None
        self.error: str | None = None
        # Candidate payload (built in migrate_entity, written in Phase B).
        self.rebuilt_rows: list[dict[str, Any]] | None = None
        self.events: dict[str, list] | None = None
        self.source_movement: float = 0.0
        self.upward_offsets: float = 0.0
        self.post_movement: float = 0.0
        self.ge_post_movement: float = 0.0
        self.metadata: dict[str, Any] | None = None


_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


async def migrate_entity(
    ws: HAWebSocket,
    givtcp_id: str,
    ge_id: str,
    description: str,
    cutover: datetime,
    ge_unit: str,
    ge_known: bool,
    reset_class: ResetClass,
    tz: ZoneInfo,
    trust_source_sums: bool,
    warn_diverged: bool = False,
    max_kwh: float | None = None,
) -> MigrationResult:
    r = MigrationResult(description, ge_id, warn_diverged)

    try:
        raw_givtcp = await ws.get_statistics([givtcp_id], _EPOCH, end=cutover)
        raw_ge = await ws.get_statistics([ge_id], _EPOCH)
    except Exception as exc:
        r.status = "error"
        r.error = str(exc)
        return r

    givtcp_stats = [_normalise(s) for s in raw_givtcp.get(givtcp_id, [])]
    ge_all = [_normalise(s) for s in raw_ge.get(ge_id, [])]

    ge_pre = [s for s in ge_all if _to_utc(s["start"]) < cutover]
    ge_post = [s for s in ge_all if _to_utc(s["start"]) >= cutover]

    r.givtcp_rows = len(givtcp_stats)
    r.ge_pre_rows = len(ge_pre)
    r.ge_post_rows = len(ge_post)

    if not givtcp_stats:
        r.status = "no_givtcp_data"
        return r

    # Safety guard: the target must have been recognised by the registry resolver
    # (`ge_known`). If it wasn't, resolution produced a phantom — clearing it is a
    # no-op and importing would create (or overwrite) an orphan series nothing
    # references, while the real entity stays un-migrated. This trusts registry
    # recognition, not recorder presence: an orphan a prior broken run left at the
    # canonical id is in the recorder but is still not a real target. Refuse it.
    if not ge_known:
        r.status = "ge_not_found"
        return r

    # A real target with no pre-cutover history means GivTCP and GE never
    # overlapped before the boundary, so there's nothing to anchor the rebase
    # against at the seam. Safe (the rebase still makes GE continue from GivTCP),
    # but worth surfacing — flag it without blocking.
    r.warn_no_ge_pre = r.ge_pre_rows == 0

    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": ge_id,
        "unit_of_measurement": ge_unit,
    }

    if trust_source_sums:
        # Legacy path: copy GivTCP sums + rebase GE-post (unchanged behaviour).
        last_givtcp_sum = givtcp_stats[-1].get("sum") or 0.0
        r.sum_at_cutover = last_givtcp_sum
        rebased_post = rebase_sum(ge_post, last_givtcp_sum)
        merged = givtcp_stats + rebased_post
        r.rebuilt_rows = merged
        r.metadata = metadata
    else:
        # Rebuild path (default): one continuous sum from the concatenated state
        # timeline, plausibility- and reset-guarded.
        merged_states = build_merged_states(givtcp_stats, ge_all, cutover)
        deltas = [
            (merged_states[i]["state"] - merged_states[i - 1]["state"])
            for i in range(1, len(merged_states))
            if merged_states[i].get("state") is not None
            and merged_states[i - 1].get("state") is not None
        ]
        # Negative deltas (resets, spikes) are silently excluded inside
        # adaptive_ceiling (it filters to `d > 0`), so passing the full list here
        # is intentional — no pre-filtering needed.
        adaptive = adaptive_ceiling(deltas)
        ceiling = effective_ceiling(adaptive, max_kwh)
        if ceiling is None:
            # Too little clean data to estimate a guard and no --max-kw cap given:
            # refuse rather than import an unguarded sum.
            r.status = "insufficient_data"
            return r
        events: dict[str, list] = {}
        merged = rebuild_sum_walk(merged_states, reset_class, ceiling, tz, events=events)
        r.rebuilt_rows = merged
        r.events = events
        # reset-aware source movement over the pre-cutover window, the UPWARD
        # offsets the rebuild suppressed (cleaned comparison), and the rebuilt
        # movement over the post-cutover window (GE-preservation check)
        pre = [s for s in merged_states if _to_utc(s["start"]) < cutover]
        r.source_movement = _reset_aware_movement(pre, reset_class, tz)
        r.upward_offsets = sum(e["offset"] for e in events.get("rebaseline", []) if e["offset"] > 0)
        post = [s for s in merged if _to_utc(s["start"]) >= cutover]
        r.post_movement = _reset_aware_movement(post, reset_class, tz)
        # original GE rows over the SAME post-cutover window, for preservation check
        r.ge_post_movement = _reset_aware_movement(ge_post, reset_class, tz)
        r.sum_at_cutover = next(
            (row["sum"] for row in merged if _to_utc(row["start"]) >= cutover), None
        )
        r.metadata = metadata
    r.merged_rows = len(merged)
    r.status = "candidate"  # built, not yet validated/written
    return r


# ---------------------------------------------------------------------------
# Post-migration validation + opt-in residue repair
# ---------------------------------------------------------------------------


def _repairable(ge_id: str, units_by_id: dict[str, str | None], implausible: list) -> bool:
    """Return True only when *ge_id* is a sum entity AND has implausible findings.

    Mean entities (power, SOC, temperature) are not present in ``units_by_id``
    (which is built exclusively from the sum migration plan).  Admitting them to
    the repair path would clear their mean series and re-import them as a sum
    series, corrupting the data.  This guard is the single enforcement point.
    """
    return bool(implausible) and ge_id in units_by_id


def _repair_reset_class(
    ge_id: str,
    reset_classes: dict[str, ResetClass] | None,
) -> ResetClass:
    """Reset class for residue repair: prefer the migration plan's authoritative
    class (keyed by resolved ge_id), falling back to suffix inference. The plan
    value survives user-renamed entity IDs that no longer carry a known suffix.
    """
    return (reset_classes or {}).get(ge_id) or classify_entity(ge_id)


def _state_deltas(rows: list[dict[str, Any]]) -> list[float]:
    """Positive-or-any consecutive state deltas, mirroring migrate_entity's pattern."""
    return [
        rows[i]["state"] - rows[i - 1]["state"]
        for i in range(1, len(rows))
        if rows[i].get("state") is not None and rows[i - 1].get("state") is not None
    ]


def _gap_undercount_intervals(events: dict[str, list] | None) -> list[tuple[str, str]]:
    """The covered intervals for a candidate's accepted reset gaps.

    Each ``gap_undercount`` event carries ``from`` (the prior reading's start) and
    ``start`` (the post-gap reading); the carried-flat interval is ``[from, start]``.
    """
    out: list[tuple[str, str]] = []
    for e in (events or {}).get("gap_undercount", []):
        frm, start = e.get("from"), e.get("start")
        if frm and start:
            out.append((frm, start))
    return out


async def apply_residue_repair(
    results: list[MigrationResult],
    units_by_id: dict[str, str | None] | None,
    reset_classes: dict[str, ResetClass] | None,
    tz: ZoneInfo,
    max_kwh: float | None = None,
) -> list[str]:
    """Re-walk any sum candidate that still carries implausible hours, MUTATE-ONLY.

    Only sum entities with residual implausible findings are touched
    (``_repairable``). The re-walked rows REPLACE ``r.rebuilt_rows`` in place, so
    the candidate that Phase B's :func:`write_candidate` later writes (and Phase C
    verifies) is the repaired one. This performs NO WebSocket writes itself —
    Phase B remains the sole writer, keeping the two-phase gate's invariant that a
    single gated write path exists. The caller must invoke this ONLY after the
    apply gate has proven the whole candidate set non-blocking. Returns the
    repaired ids.
    """
    units_by_id = units_by_id or {}
    repaired: list[str] = []
    for r in results:
        if r.status != "candidate":
            continue
        rows = r.rebuilt_rows or []
        if not rows:
            continue
        ceiling = effective_ceiling(adaptive_ceiling(_state_deltas(rows)), max_kwh)
        if ceiling is None:
            continue
        implausible = find_implausible_hours(rows, ceiling)
        if not _repairable(r.ge_id, units_by_id, implausible):
            continue
        rc = _repair_reset_class(r.ge_id, reset_classes)
        r.rebuilt_rows = rebuild_sum_walk(rows, rc, ceiling, tz)
        repaired.append(r.ge_id)
    return repaired


async def run_validation(
    ws: HAWebSocket,
    results: list[MigrationResult],
    tz: ZoneInfo,
    units_by_id: dict[str, str | None] | None = None,
    reset_classes: dict[str, ResetClass] | None = None,
    applying: bool = False,
    max_kwh: float | None = None,
    cutover: datetime | None = None,
) -> tuple[int, bool]:
    """Validate each built **candidate** (``r.rebuilt_rows``) and report findings.

    Operates on the in-memory candidate — it does NOT re-read HA — so the same
    findings drive both the dry-run preview and the Phase-A apply gate. Per
    candidate it records: ``gaps`` (informational); ``flat_lines`` clipped by the
    accepted ``gap_undercount`` intervals to the residual *unexplained* portions
    (a residual portion >= ``_FLAT_LINE_MIN_HOURS`` is blocking); a
    ``source_comparison`` against the cleaned pre-cutover source movement; a
    ``ge_preservation`` comparison of the post-cutover rebuilt vs GE-source
    movement; and the raw rebuild ``events`` (``rebaseline``/``smear``/
    ``gap_undercount`` are accepted, ``unresolved`` blocks).

    ``applying`` selects the report header: under ``--apply`` the findings describe
    the candidates about to be written ("candidates to write"); otherwise they are
    a dry-run preview of the current series.

    This is a read-only validation pass: residue repair is a separate, gated step
    (:func:`apply_residue_repair`) the caller runs *after* the apply gate, so no
    write of any kind ever precedes the gate.

    Returns ``(exit_code, blocking)``: the report exit code, and a blocking flag
    true when any candidate has an unexplained flat >= threshold, a flagged
    source comparison, a flagged GE-preservation comparison, or an unresolved
    held run.
    """
    units_by_id = units_by_id or {}
    findings: dict[str, dict[str, list]] = {}
    series_by_id: dict[str, list[dict[str, Any]]] = {}
    blocking = False

    for r in results:
        if r.status != "candidate":
            continue
        rows = r.rebuilt_rows or []
        if not rows:
            continue
        events = r.events or {}
        gaps = classify_gaps(rows)

        # Residual unexplained flats: clip each flat span by the accepted
        # gap_undercount intervals, keep contiguous residual >= threshold.
        covered = _gap_undercount_intervals(events)
        flat_lines: list[dict[str, Any]] = []
        for span in find_flat_line_spans(rows):
            for piece in _unexplained_flat_portions(span, covered):
                if piece["hours"] >= _FLAT_LINE_MIN_HOURS:
                    flat_lines.append(piece)

        # Pre-cutover candidate movement vs cleaned source movement.
        rc = _repair_reset_class(r.ge_id, reset_classes)
        if cutover is not None:
            pre = [s for s in rows if _to_utc(s["start"]) < cutover]
        else:
            pre = rows
        candidate_pre_movement = _reset_aware_movement(pre, rc, tz)
        source_comparison = compare_source_movement(
            r.source_movement, candidate_pre_movement, r.upward_offsets
        )

        # Post-cutover GE-preservation: rebuilt movement must match GE source.
        ge_preservation = compare_source_movement(r.ge_post_movement, r.post_movement, 0.0)

        unresolved = events.get("unresolved", [])
        candidate_blocking = bool(flat_lines or unresolved) or bool(
            source_comparison["flagged"] or ge_preservation["flagged"]
        )
        blocking = blocking or candidate_blocking

        ceiling = effective_ceiling(adaptive_ceiling(_state_deltas(rows)), max_kwh)
        implausible = find_implausible_hours(rows, ceiling) if ceiling is not None else []
        fake_resets = find_fake_reset_shapes(rows, ceiling) if ceiling is not None else []

        findings[r.ge_id] = {
            "implausible": implausible,
            "fake_resets": fake_resets,
            "gaps": gaps,
            "flat_lines": flat_lines,
            "source_comparison": source_comparison,
            "ge_preservation": ge_preservation,
            "rebaseline": events.get("rebaseline", []),
            "smear": events.get("smear", []),
            "gap_undercount": events.get("gap_undercount", []),
            "unresolved": unresolved,
        }
        series_by_id[r.ge_id] = rows

    duplicates = find_duplicate_series(_dedup_series(series_by_id, units_by_id))
    mode = "candidates" if applying else "dry-run"
    text, exit_code = format_validation_report(findings, duplicates, mode=mode)
    print(text)

    return exit_code, blocking


async def write_candidate(ws: HAWebSocket, r: MigrationResult) -> None:
    """Phase B: clear + import one approved candidate. Not transactional across
    entities — the caller aborts + reports on the first failure (backup recovers)."""
    await ws.clear_statistics([r.ge_id])
    await ws.import_statistics(metadata=r.metadata, stats=r.rebuilt_rows)


async def verify_written(ws: HAWebSocket, r: MigrationResult) -> bool:
    """Phase C: re-read the stored series and confirm it matches the approved
    candidate — row count, per-row normalized `start` timestamp, AND per-row sum
    within epsilon (equal sums with shifted/reordered timestamps must NOT pass)."""
    raw = await ws.get_statistics([r.ge_id], _EPOCH)
    stored = [_normalise(s) for s in raw.get(r.ge_id, [])]
    rebuilt = r.rebuilt_rows or []
    if len(stored) != len(rebuilt):
        return False
    return all(
        a["start"] == b["start"]
        and abs((a.get("sum") or 0.0) - (b.get("sum") or 0.0)) <= _FLAT_EPSILON
        for a, b in zip(stored, rebuilt)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_cutover_suggestion(last_givtcp: date | None, first_ge: date | None) -> None:
    """Print the detected dates and a suggested --cutover (auto-detect mode)."""
    print("  Last GivTCP data point  :", last_givtcp or "not found")
    print("  First givenergy_local   :", first_ge or "not found")
    print()
    if last_givtcp and first_ge:
        # Suggest the later of the two dates — the tail of the overlap window. The
        # boundary is midnight at the *start* of this date, so GivTCP history
        # through the previous day is migrated and GE takes over from 00:00 on the
        # suggested date regardless of when GivTCP actually stopped that day.
        suggested = max(last_givtcp, first_ge)
        print(f"  Suggested --cutover date: {suggested}")
        print()
        print(f"  GivTCP data through {suggested - timedelta(days=1)} 23:59 UTC → migrated")
        print(f"  givenergy_local data from {suggested} 00:00 UTC    → kept")
        print(f"  givenergy_local data before {suggested} 00:00 UTC  → discarded")
        print()
        print("  Rerun with --cutover YYYY-MM-DD to preview the migration,")
        print("  then add --apply to write the changes.")
    elif last_givtcp:
        print("  givenergy_local has no statistics yet — install it and let it")
        print("  run for at least one full day before migrating.")
    else:
        print("  GivTCP statistics not found. Nothing to migrate.")


def _build_plan(
    inv_serials: list[str],
    batt_serials: list[str],
    meta_by_id: dict[str, dict[str, Any]],
    include_charge_from_grid: bool,
    canonical_to_actual: dict[str, str],
) -> list[tuple[str, str, str, str, bool, ResetClass, bool]]:
    """Construct the per-entity migration plan from the detected serials.

    Each entry's final field records whether the canonical givenergy_local
    target was **recognised by the registry resolver** (a key in
    `canonical_to_actual`). That is the only trustworthy proof the target maps to
    a real entity: a statistic merely present in the recorder is not — a prior
    run of the buggy area-prefix version may have left an orphan at the canonical
    id. HA 2026.6 area prefixes and user renames are followed via the map. The
    penultimate field is the entity's `ResetClass`, derived from its
    givenergy_local suffix, driving reset-vs-corruption calls in the sum rebuild.
    """
    plan: list[tuple[str, str, str, str, bool, ResetClass, bool]] = []
    for sn in inv_serials:
        for gt_sfx, ge_sfx, desc, default in INVERTER_PAIRS:
            if not default and not include_charge_from_grid:
                continue
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            canonical = f"sensor.givenergy_inverter_{sn}_{ge_sfx}"
            resolved = canonical in canonical_to_actual
            ge_id = canonical_to_actual.get(canonical, canonical)
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or "kWh"
            reset_class = classify_entity(ge_sfx)
            plan.append((givtcp_id, ge_id, desc, unit, not default, reset_class, resolved))
    for sn in batt_serials:
        for gt_sfx, ge_sfx, desc, fallback_unit in BATTERY_PAIRS:
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            canonical = f"sensor.givenergy_battery_{sn}_{ge_sfx}"
            resolved = canonical in canonical_to_actual
            ge_id = canonical_to_actual.get(canonical, canonical)
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or fallback_unit
            reset_class = classify_entity(ge_sfx)
            plan.append((givtcp_id, ge_id, desc, unit, False, reset_class, resolved))
    return plan


def _systemic_resolution_failure(
    plan: list[tuple[str, str, str, str, bool, ResetClass, bool]],
) -> list[str]:
    """Return the targets to abort on iff the resolver mapped *none* of them.

    Each entry's final field is the registry-recognition flag from `_build_plan`
    — the only trustworthy proof of resolution. A partial miss is not fatal: a
    target's GivTCP source may be irrelevant, or the entity is gated off on this
    topology (e.g. `single_phase_only` sensors — PV Energy Today, House
    Consumption Today — are never created on a three-phase inverter). Those are
    skipped per-entity (`ge_not_found`) without blocking the rest. But if NOT ONE
    target was recognised, the resolver failed wholesale and `--apply` would
    rewrite only phantoms (including any orphan a prior broken run left in the
    recorder — which is exactly why this never consults the recorder). Surface
    them all for an up-front abort; returns [] as soon as one target resolves.
    """
    if any(resolved for (*_rest, resolved) in plan):
        return []
    return [ge_id for (_givtcp, ge_id, *_rest) in plan]


def _print_summary(results: list[MigrationResult], applying: bool) -> int:
    """Print the results table + counts; return the process exit code."""
    width = 46
    print()
    print("─" * 88)
    print(f"  {'Sensor':<{width}} {'GivTCP':>8} {'GE pre':>7} {'GE post':>8}  Result")
    print("─" * 88)
    for r in results:
        warn_tag = ""
        if r.status in ("migrated", "dry_run"):
            if r.warn_diverged:
                warn_tag += "  ⚠️  verify values"
            if r.warn_no_ge_pre:
                warn_tag += "  ⚠️  no GE overlap"
        print(
            f"  {r.description:<{width}} {r.givtcp_rows:>8} {r.ge_pre_rows:>7}"
            f" {r.ge_post_rows:>8}  {r.status}{warn_tag}"
        )
    print("─" * 88)

    counts = Counter(r.status for r in results)
    errored = counts["error"]
    not_found = counts["ge_not_found"]
    insufficient = counts["insufficient_data"]
    tail = (
        f"No GivTCP data: {counts['no_givtcp_data']}  |  "
        f"GE not found: {not_found}  |  Insufficient data: {insufficient}  |  "
        f"Errors: {errored}"
    )

    if not applying:
        print(f"\n  Planned: {counts['dry_run']}  |  {tail}")
        print("  Add --apply to write changes (back up your DB first).")
    else:
        print(f"\n  Migrated: {counts['migrated']}  |  {tail}")

    for r in results:
        if r.status == "error":
            print(f"\n  ERROR — {r.description}: {r.error}", file=sys.stderr)
        elif r.status == "ge_not_found":
            print(
                f"\n  ⚠️  {r.description}: givenergy_local target {r.ge_id} was not resolved "
                "from the entity registry (area prefix / rename unmapped, or no such entity). "
                "Skipped.",
                file=sys.stderr,
            )
        elif r.status == "insufficient_data":
            print(
                f"\n  ⚠️  {r.description}: too little clean history to estimate a "
                "plausibility ceiling, and no --max-kw cap was given — skipped rather "
                "than import an unguarded sum. Re-run with --max-kw <kW above the "
                "largest legitimate hourly delta across all your counters — grid "
                "import and battery charging can exceed PV output> (or "
                "--trust-source-sums if the GivTCP sums are known-good).",
                file=sys.stderr,
            )

    return 0 if (errored == 0 and not_found == 0 and insufficient == 0) else 2


async def run(args: argparse.Namespace) -> int:
    applying = args.apply

    mode = "APPLY — will modify statistics" if applying else "DRY RUN — nothing will be written"
    print(f"Home Assistant : {args.ha_url}")
    print(f"Mode           : {mode}")
    print()

    if _apply_requires_cap(applying, args.max_kw):
        print(
            "\n  ✋ --apply requires --max-kw. The destructive rebuild needs a "
            "trusted upper bound on a plausible hourly delta (kW) — the adaptive "
            "ceiling alone can be skewed by heavy corruption. Run a dry-run first "
            "to see the plan, then re-run with --apply --max-kw <kW above your "
            "largest legitimate hourly delta across all counters>.",
            file=sys.stderr,
        )
        return 2

    print("Connecting …", end=" ", flush=True)
    ws = HAWebSocket(args.ha_url, args.token)
    try:
        await ws.connect()
    except Exception as exc:
        print(f"FAILED\n{exc}", file=sys.stderr)
        return 1
    print("OK")

    # Local timezone drives reset-boundary detection in the sum rebuild.
    tz = await ws.get_timezone()

    # Discover serials
    all_meta = await ws.list_statistic_ids()
    meta_by_id: dict[str, dict[str, Any]] = {m["statistic_id"]: m for m in all_meta}
    all_ids = list(meta_by_id)

    inv_serials = sorted({m.group(1).lower() for s in all_ids if (m := _INV_DETECT.match(s))})
    batt_serials = sorted({m.group(1).lower() for s in all_ids if (m := _BATT_DETECT.match(s))})

    if not inv_serials:
        print(
            "\nNo GivTCP inverter statistics found in the recorder DB.\n"
            "Check that GivTCP was running on this HA instance and that the\n"
            "recorder has not been fully purged."
        )
        await ws.close()
        return 0

    print(f"Inverter serials : {', '.join(inv_serials)}")
    print(f"Battery serials  : {', '.join(batt_serials) or '(none found)'}")

    # Resolve canonical givenergy_local target ids to the real recorder ids,
    # following HA 2026.6 area prefixes and user renames.
    entity_entries = await ws.list_entity_registry()
    device_entries = await ws.list_device_registry()
    canonical_to_actual = build_entity_id_resolver(entity_entries, device_entries)

    def resolve(eid: str) -> str:
        return canonical_to_actual.get(eid, eid)

    # Cut-over detection / validation
    if args.cutover is None:
        print("\nDetecting cut-over date …", end=" ", flush=True)
        last_givtcp, first_ge = await detect_cutover(ws, inv_serials[0], resolve)
        print("done")
        print()
        _print_cutover_suggestion(last_givtcp, first_ge)
        await ws.close()
        return 0

    # The boundary is midnight at the *start* of the cutover date. GivTCP history
    # strictly before that moment is migrated; givenergy_local data from that
    # moment onwards is kept. If GivTCP stopped mid-day on the cutover date, GE
    # picks up from 00:00 that day and covers the full day cleanly.
    cutover_date = date.fromisoformat(args.cutover)
    cutover = datetime.combine(cutover_date, datetime.min.time(), tzinfo=UTC)
    print(f"Cut-over date    : {cutover_date} 00:00 UTC")

    plan = _build_plan(
        inv_serials, batt_serials, meta_by_id, args.include_charge_from_grid, canonical_to_actual
    )

    # Pre-flight guard: if the registry resolver mapped NO target, --apply would
    # only write phantom series while the real entities stay un-migrated. Refuse
    # up front rather than do that silently. A partial miss (some targets gated
    # off on this topology, e.g. single_phase_only sensors) is left to the
    # per-entity path, which skips each as `ge_not_found` without blocking the
    # rest. The check trusts registry recognition, never the recorder — an orphan
    # from a prior broken run must not pass for a real, resolved target.
    missing_targets = _systemic_resolution_failure(plan)
    if applying and missing_targets:
        print()
        print("  ✋ Refusing to --apply — not one givenergy_local target was resolved from")
        print("     the entity registry, so entity-id resolution has failed (an area")
        print("     prefix or rename the script couldn't map). Affected targets:")
        for ge_id in missing_targets:
            print(f"       {ge_id}")
        print()
        print("     Writing would create phantom series and leave your real entities")
        print("     un-migrated. Re-run without --apply to inspect the full table.")
        await ws.close()
        return 2

    if applying:
        print()
        print("  ⚠️  This will CLEAR and REWRITE long-term statistics for the")
        print("  ⚠️  listed givenergy_local entities. Back up your recorder")
        print("  ⚠️  database before proceeding.")
        print("  ℹ️  Re-running with the SAME or a LATER --cutover is safe and")
        print("  ℹ️  idempotent (the script always re-reads the original GivTCP entity")
        print("  ℹ️  for pre-cutover data). An EARLIER cutover is not — it would fold")
        print("  ℹ️  already-migrated history back through the rebase. If you use")
        print("  ℹ️  today's date, the day's stats are still live, so wait until")
        print("  ℹ️  tomorrow before re-running.")
        try:
            confirm = input("  Type 'yes' to continue: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("Aborted.")
            await ws.close()
            return 1

    print()

    # ── Phase A: build every candidate (no writes yet) ──────────────────────
    results: list[MigrationResult] = []
    units_by_id: dict[str, str | None] = {}
    # Authoritative reset cadence per target, straight from the plan — used by the
    # residue-repair walk instead of re-deriving from the (renameable) ge_id.
    reset_classes = {ge_id: reset_class for (_, ge_id, _, _, _, reset_class, _) in plan}
    for givtcp_id, ge_id, desc, unit, warn, reset_class, resolved in plan:
        units_by_id[ge_id] = unit
        print(f"  Building: {desc} …", end=" ", flush=True)
        # `resolved` (registry recognition), not recorder presence, is what makes
        # a target real — so an orphan from a prior broken run is never written.
        r = await migrate_entity(
            ws,
            givtcp_id,
            ge_id,
            desc,
            cutover,
            unit,
            resolved,
            reset_class,
            tz,
            args.trust_source_sums,
            warn,
            max_kwh=args.max_kw,
        )
        results.append(r)
        print(r.status)

    # Validate the in-memory candidates and print the findings report. This drives
    # both the dry-run preview and the Phase-A apply gate from the SAME findings.
    # Validation is read-only, so no write of any kind precedes the gate below —
    # residue repair (if opted in) happens in Phase B, only once the set is clean.
    validation_exit, blocking = await run_validation(
        ws,
        results,
        tz,
        units_by_id=units_by_id,
        reset_classes=reset_classes,
        applying=applying,
        max_kwh=args.max_kw,
        cutover=cutover,
    )

    # Apply gate: if any candidate has a blocking finding, write NOTHING.
    if applying and blocking:
        print(
            "\n  ✋ Refusing to --apply — validation found blocking issues above "
            "(unexplained flat span, source-movement divergence, post-cutover "
            "GE-divergence, or an unresolved rebuild run). Nothing was written. "
            "Investigate the flagged entities, then re-run.",
            file=sys.stderr,
        )
        await ws.close()
        return max(2, validation_exit)

    candidates = [r for r in results if r.status == "candidate"]

    # Dry-run stops here: the candidates were never meant to be written.
    if not applying:
        for r in candidates:
            r.status = "dry_run"
        summary_code = _print_summary(results, applying)
        await ws.close()
        return max(summary_code, validation_exit)

    # ── Phase B: write all approved candidates, or abort on the first failure ──
    # Gated residue repair: only now, with the whole set proven non-blocking, do we
    # re-walk the repairable candidates IN PLACE (no write). The Phase B loop below
    # then writes each repaired candidate exactly once, so Phase B is the sole
    # writer and never runs ahead of the gate (the invariant Task 9 protects).
    if args.repair_residue:
        repaired = await apply_residue_repair(
            results, units_by_id, reset_classes, tz, max_kwh=args.max_kw
        )
        if repaired:
            print(
                f"  Repaired {len(repaired)} entit{'y' if len(repaired) == 1 else 'ies'} with"
                " residual implausible hours: " + ", ".join(repaired)
            )

    written: list[str] = []
    for idx, r in enumerate(candidates):
        try:
            await write_candidate(ws, r)
        except Exception as exc:
            still_clean = [r2.ge_id for r2 in candidates[idx + 1 :]]
            print(
                f"\n  ✋ Write FAILED on {r.ge_id}: {exc}",
                file=sys.stderr,
            )
            print(
                "     Fully written : " + (", ".join(written) or "(none)"),
                file=sys.stderr,
            )
            print(
                f"     Mid-write     : {r.ge_id} (cleared, import may be incomplete)",
                file=sys.stderr,
            )
            print(
                "     Not touched   : " + (", ".join(still_clean) or "(none)"),
                file=sys.stderr,
            )
            print(
                "     Restore your recorder database from the backup you took "
                "before --apply, then investigate before re-running.",
                file=sys.stderr,
            )
            await ws.close()
            return 1
        written.append(r.ge_id)
        # Pace writes so the single-threaded recorder can drain between entities.
        if idx < len(candidates) - 1:
            await asyncio.sleep(_ENTITY_PAUSE_SECONDS)

    # ── Phase C: re-read each stored series and confirm it matches the candidate ──
    for r in candidates:
        try:
            ok = await verify_written(ws, r)
        except Exception as exc:
            print(
                f"\n  ✋ Post-write verification FAILED to re-read {r.ge_id}: {exc}\n"
                "     The series were written but could not be verified. Restore your "
                "recorder database from the backup you took before --apply and "
                "investigate before re-running.",
                file=sys.stderr,
            )
            await ws.close()
            return 1
        if not ok:
            print(
                f"\n  ✋ Post-write verification FAILED for {r.ge_id}: the stored "
                "series does not match the approved candidate. Restore your recorder "
                "database from the backup you took before --apply and investigate "
                "before re-running.",
                file=sys.stderr,
            )
            await ws.close()
            return 1

    # Only now is the write proven good.
    for r in candidates:
        r.status = "migrated"

    summary_code = _print_summary(results, applying)

    await ws.close()

    return max(summary_code, validation_exit)


def _positive_float(value: str) -> float:
    """argparse type: a strictly positive float (for --max-kw)."""
    f = float(value)
    if not math.isfinite(f) or f <= 0:
        raise argparse.ArgumentTypeError("must be a positive, finite number of kW")
    return f


def main() -> None:
    p = argparse.ArgumentParser(
        description="Migrate GivTCP long-term energy statistics to givenergy_local.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "See docs/migration-from-givtcp.md for the full sensor catalogue and design rationale."
        ),
    )
    p.add_argument(
        "--ha-url",
        required=True,
        help="Home Assistant base URL, e.g. http://homeassistant.local:8123",
    )
    p.add_argument(
        "--token",
        required=True,
        help="Long-Lived Access Token (Profile → Security → Long-Lived Access Tokens)",
    )
    p.add_argument(
        "--cutover",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Boundary date. GivTCP history strictly before midnight on this date "
            "is migrated; givenergy_local data from midnight onwards is kept. "
            "Choose a day when both integrations were running so GivTCP history "
            "is complete and GE covers the rest. "
            "Omit to auto-detect from recorder history."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write changes to Home Assistant. Without this flag the script runs in "
            "dry-run mode and prints a preview without modifying anything. "
            "Requires --max-kw."
        ),
    )
    p.add_argument(
        "--include-charge-from-grid",
        action="store_true",
        help=(
            "Also migrate ac_charge_energy_total_kwh → charge_from_grid_total. "
            "⚠️  These sensors read different register blocks on some inverters — "
            "inspect the imported values in the Energy dashboard afterwards."
        ),
    )
    p.add_argument(
        "--trust-source-sums",
        action="store_true",
        help=(
            "Copy GivTCP's sum column verbatim and rebase at the join, instead of "
            "rebuilding sums from state. Use only if your GivTCP sums are known-good."
        ),
    )
    p.add_argument(
        "--max-kw",
        type=_positive_float,
        default=None,
        metavar="KW",
        help=(
            "Maximum legitimate hourly energy change, in kW (= kWh per hour), "
            "applied to every migrated counter. Set it above the largest hourly "
            "delta any of your counters can see — grid import and battery charging "
            "can exceed your inverter's PV output. Under --apply this value is "
            "used directly as the authoritative rebuild bound (the adaptive p99 "
            "estimate is only the dry-run fallback). Must be positive. Required "
            "with --apply."
        ),
    )
    p.add_argument(
        "--repair-residue",
        action="store_true",
        help=(
            "After validation, clear + re-import the rebuilt series for entities "
            "with residual implausible hours. Off by default (report only)."
        ),
    )
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
