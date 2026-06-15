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
        --apply

The cut-over date is the boundary between GivTCP and givenergy_local history.
GivTCP data strictly before midnight on that date is migrated; givenergy_local
data from midnight on that date onwards is kept (and its running sum is rebased
to continue from where GivTCP left off). Any givenergy_local data before that
boundary is discarded — it will typically be partial or recorded in parallel with
GivTCP. Choosing a day when both integrations were running means the full GivTCP
history is captured and GE takes over from 00:00 that day regardless of when
GivTCP actually stopped.

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

See docs/migration-from-givtcp.md for the full sensor catalogue and design notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
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

# (givtcp_suffix, ge_suffix, description) — mean-type series (power/SOC/temp).
# Straight mean/min/max copy; no sum, no rebase, no plausibility.
# NOTE: these suffixes are provisional and must be verified against a live
# GivTCP + givenergy_local registry before relying on them in anger.
MEAN_PAIRS: list[tuple[str, str, str]] = [
    ("pv_power", "pv_power", "PV power"),
    ("grid_power", "grid_power", "Grid power (signed)"),
    ("battery_power", "battery_power", "Battery power (signed)"),
    ("load_power", "house_consumption", "House consumption power"),
    ("soc", "battery_soc", "Battery SOC (inverter)"),
]

# Per-battery mean series: sensor.givtcp_<batt_sn>_<gt> -> sensor.givenergy_battery_<batt_sn>_<ge>
# NOTE: provisional suffixes — verify against a live registry before relying on them.
MEAN_BATTERY_PAIRS: list[tuple[str, str, str]] = [
    ("soc", "soc", "Battery SOC (per pack)"),
    ("battery_temperature", "temperature", "Battery temperature"),
]

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

# Multiplier on the (normal-scaled) MAD for the plausibility ceiling. Tuned so
# genuine inverter-clip hours pass while order-of-magnitude fake spikes are
# rejected; pinned by the LTS-fixture acceptance test.
_CEILING_MAD_K = 8.0


def adaptive_ceiling(deltas: list[float | None]) -> float:
    """Robust per-hour ceiling from an entity's positive state-deltas.

    Uses median + K * 1.4826 * MAD over the positive, finite deltas. Both the
    median and the MAD are resistant to a handful of giant outliers, so the
    bound reflects genuine hourly behaviour even on a heavily corrupted series.
    Returns +inf when there is nothing positive to anchor on (caller then can't
    guard, and the walk accepts all non-negative deltas).
    """
    pos = sorted(d for d in deltas if d is not None and d > 0)
    if not pos:
        return float("inf")
    median = statistics.median(pos)
    mad = statistics.median([abs(d - median) for d in pos])
    spread = mad if mad > 0 else median
    return median + _CEILING_MAD_K * 1.4826 * spread


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


def rebuild_sum_walk(
    rows: list[dict[str, Any]],
    reset_class: ResetClass,
    ceiling: float,
    tz: ZoneInfo,
    midnight_tol_hours: float = 2.0,
) -> list[dict[str, Any]]:
    """Rebuild the ``sum`` column from ``state`` with reset/plausibility guards.

    ``rows`` are normalised (ISO ``start``, numeric or None ``state``), sorted
    ascending. Returns copies with ``sum`` set to a clean cumulative total:

    - delta in [0, ceiling]            -> accept, advance running + last-good state
    - delta < 0 at a reset boundary    -> reset: add post-reset state to running
    - delta < 0 off-boundary           -> corruption: hold last-good (state + sum)
    - delta > ceiling                  -> fake spike: hold last-good
    - missing state (gap)              -> carry running forward

    Holding last-good leaves ``prev_state`` at the last trusted reading, so the
    recovery after a transient zero/spike is measured against it (a small,
    accepted delta) instead of booking the bogus jump.
    """
    out: list[dict[str, Any]] = []
    running = 0.0
    prev_state: float | None = None
    for row in rows:
        r = dict(row)
        state = row.get("state")
        if state is None:
            r["sum"] = round(running, 6)
            out.append(r)
            continue
        if prev_state is None:
            running = float(state)
            prev_state = float(state)
            r["sum"] = round(running, 6)
            out.append(r)
            continue
        delta = state - prev_state
        if 0 <= delta <= ceiling:
            running += delta
            prev_state = state
        elif delta < 0 and _is_reset_boundary(r["start"], reset_class, tz, midnight_tol_hours):
            running += state
            prev_state = state
        # else: corruption (off-boundary drop) or spike (> ceiling) -> hold last-good.
        r["sum"] = round(running, 6)
        out.append(r)
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
        if b <= a * 0.05 and (c - b) > ceiling:
            shapes.append({"start": rows[i]["start"], "recovery": round(c - b, 3)})
    return shapes


def format_validation_report(
    findings: dict[str, dict[str, list]],
    duplicates: list[tuple[str, str]],
) -> tuple[str, int]:
    """Render the post-migration validation findings; return (text, exit_code).

    exit_code is non-zero when substantive issues exist (implausible hours,
    fake-reset shapes, or duplicate series). Gaps are reported informationally
    and do not affect the exit code.
    """
    lines = ["", "Validation report", "─" * 72]
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


_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def mean_metadata(ge_id: str, unit: str) -> dict[str, Any]:
    return {
        "has_mean": True,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": ge_id,
        "unit_of_measurement": unit,
    }


async def migrate_entity(
    ws: HAWebSocket,
    givtcp_id: str,
    ge_id: str,
    description: str,
    cutover: datetime,
    ge_unit: str,
    apply: bool,
    ge_known: bool,
    reset_class: ResetClass,
    tz: ZoneInfo,
    trust_source_sums: bool,
    warn_diverged: bool = False,
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

    if trust_source_sums:
        # Legacy path: copy GivTCP sums + rebase GE-post (unchanged behaviour).
        last_givtcp_sum = givtcp_stats[-1].get("sum") or 0.0
        r.sum_at_cutover = last_givtcp_sum
        rebased_post = rebase_sum(ge_post, last_givtcp_sum)
        merged = givtcp_stats + rebased_post
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
        ceiling = adaptive_ceiling(deltas)
        merged = rebuild_sum_walk(merged_states, reset_class, ceiling, tz)
        r.sum_at_cutover = next(
            (row["sum"] for row in merged if _to_utc(row["start"]) >= cutover), None
        )
    r.merged_rows = len(merged)

    if not apply:
        r.status = "dry_run"
        return r

    try:
        await ws.clear_statistics([ge_id])
        await ws.import_statistics(
            metadata={
                "has_mean": False,
                "has_sum": True,
                "name": None,
                "source": "recorder",
                "statistic_id": ge_id,
                "unit_of_measurement": ge_unit,
            },
            stats=merged,
        )
        r.status = "migrated"
    except Exception as exc:
        r.status = "error"
        r.error = str(exc)

    return r


async def migrate_mean_entity(
    ws: HAWebSocket,
    givtcp_id: str,
    ge_id: str,
    description: str,
    cutover: datetime,
    ge_unit: str,
    apply: bool,
    ge_known: bool,
) -> MigrationResult:
    r = MigrationResult(description, ge_id)
    try:
        raw_givtcp = await ws.get_statistics(
            [givtcp_id], _EPOCH, end=cutover, types=["mean", "min", "max"]
        )
        raw_ge = await ws.get_statistics([ge_id], _EPOCH, types=["mean", "min", "max"])
    except Exception as exc:
        r.status = "error"
        r.error = str(exc)
        return r
    givtcp_stats = [_normalise(s) for s in raw_givtcp.get(givtcp_id, [])]
    ge_all = [_normalise(s) for s in raw_ge.get(ge_id, [])]
    ge_post = [s for s in ge_all if _to_utc(s["start"]) >= cutover]
    r.givtcp_rows = len(givtcp_stats)
    r.ge_pre_rows = len([s for s in ge_all if _to_utc(s["start"]) < cutover])
    r.ge_post_rows = len(ge_post)
    if not givtcp_stats:
        r.status = "no_givtcp_data"
        return r
    if not ge_known:
        r.status = "ge_not_found"
        return r
    merged = givtcp_stats + ge_post
    r.merged_rows = len(merged)
    if not apply:
        r.status = "dry_run"
        return r
    try:
        await ws.clear_statistics([ge_id])
        await ws.import_statistics(metadata=mean_metadata(ge_id, ge_unit), stats=merged)
        r.status = "migrated"
    except Exception as exc:
        r.status = "error"
        r.error = str(exc)
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


def _state_deltas(rows: list[dict[str, Any]]) -> list[float]:
    """Positive-or-any consecutive state deltas, mirroring migrate_entity's pattern."""
    return [
        rows[i]["state"] - rows[i - 1]["state"]
        for i in range(1, len(rows))
        if rows[i].get("state") is not None and rows[i - 1].get("state") is not None
    ]


async def run_validation(
    ws: HAWebSocket,
    results: list[MigrationResult],
    tz: ZoneInfo,
    units_by_id: dict[str, str | None] | None = None,
    repair: bool = False,
) -> int:
    """Re-read each migrated/previewed sum series, report validation findings.

    Read-only by default — runs in both dry-run and apply. In dry-run mode the
    series read back are the *current* (unmigrated) HA series, not a
    post-migration result.  When ``repair`` is set (only when --apply and
    --repair-residue are both given), entities with residual implausible hours
    have their rebuilt series cleared and re-imported; ``units_by_id`` supplies
    the unit of measurement for the re-import metadata.  Only sum entities
    (those present in ``units_by_id``) are eligible for repair — mean entities
    are never touched.
    Returns the validation exit code from ``format_validation_report``.
    """
    units_by_id = units_by_id or {}
    findings: dict[str, dict[str, list]] = {}
    series_by_id: dict[str, list[dict[str, Any]]] = {}
    to_repair: list[str] = []

    for r in results:
        if r.status not in ("migrated", "dry_run"):
            continue
        try:
            raw = await ws.get_statistics([r.ge_id], _EPOCH)
        except Exception:  # nosec B112 — validation is best-effort; skip unreadable series
            continue
        rows = [_normalise(s) for s in raw.get(r.ge_id, [])]
        if not rows:
            continue
        ceiling = adaptive_ceiling(_state_deltas(rows))
        implausible = find_implausible_hours(rows, ceiling)
        fake_resets = find_fake_reset_shapes(rows, ceiling)
        gaps = classify_gaps(rows)
        findings[r.ge_id] = {
            "implausible": implausible,
            "fake_resets": fake_resets,
            "gaps": gaps,
        }
        series_by_id[r.ge_id] = rows
        if repair and _repairable(r.ge_id, units_by_id, implausible):
            to_repair.append(r.ge_id)

    duplicates = find_duplicate_series(series_by_id)
    text, exit_code = format_validation_report(findings, duplicates)
    print(text)

    if repair and to_repair:
        print()
        print(
            f"  Repairing {len(to_repair)} entit{'y' if len(to_repair) == 1 else 'ies'} with"
            " residual implausible hours …"
        )
        for ge_id in to_repair:
            rows = series_by_id[ge_id]
            ceiling = adaptive_ceiling(_state_deltas(rows))
            # classify_entity keys off the suffix via endswith, so the full
            # resolved ge_id (…_today / …_total / …_this_year) classifies correctly.
            rebuilt = rebuild_sum_walk(rows, classify_entity(ge_id), ceiling, tz)
            unit = units_by_id.get(ge_id)
            await ws.clear_statistics([ge_id])
            await ws.import_statistics(
                metadata={
                    "has_mean": False,
                    "has_sum": True,
                    "name": None,
                    "source": "recorder",
                    "statistic_id": ge_id,
                    "unit_of_measurement": unit,
                },
                stats=rebuilt,
            )
            print(f"    repaired {ge_id}")

    return exit_code


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


def _build_mean_plan(
    inv_serials: list[str],
    batt_serials: list[str],
    meta_by_id: dict[str, dict[str, Any]],
    canonical_to_actual: dict[str, str],
) -> list[tuple[str, str, str, str, bool]]:
    """Construct the mean-series migration plan from the detected serials.

    Mirrors `_build_plan`'s canonical→actual resolution but for the mean tables
    (power / SOC / temperature). Each entry's final field records whether the
    canonical givenergy_local target was recognised by the registry resolver —
    the same trust signal `_build_plan` uses. Units come from the live metadata
    where available, falling back to a sensible default per series shape.
    """
    plan: list[tuple[str, str, str, str, bool]] = []
    for sn in inv_serials:
        for gt_sfx, ge_sfx, desc in MEAN_PAIRS:
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            canonical = f"sensor.givenergy_inverter_{sn}_{ge_sfx}"
            resolved = canonical in canonical_to_actual
            ge_id = canonical_to_actual.get(canonical, canonical)
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or "W"
            plan.append((givtcp_id, ge_id, desc, unit, resolved))
    for sn in batt_serials:
        for gt_sfx, ge_sfx, desc in MEAN_BATTERY_PAIRS:
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            canonical = f"sensor.givenergy_battery_{sn}_{ge_sfx}"
            resolved = canonical in canonical_to_actual
            ge_id = canonical_to_actual.get(canonical, canonical)
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or "%"
            plan.append((givtcp_id, ge_id, desc, unit, resolved))
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
    tail = (
        f"No GivTCP data: {counts['no_givtcp_data']}  |  "
        f"GE not found: {not_found}  |  Errors: {errored}"
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

    return 0 if (errored == 0 and not_found == 0) else 2


async def run(args: argparse.Namespace) -> int:
    applying = args.apply

    mode = "APPLY — will modify statistics" if applying else "DRY RUN — nothing will be written"
    print(f"Home Assistant : {args.ha_url}")
    print(f"Mode           : {mode}")
    print()

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

    # Execute
    results: list[MigrationResult] = []
    units_by_id: dict[str, str | None] = {}
    for idx, (givtcp_id, ge_id, desc, unit, warn, reset_class, resolved) in enumerate(plan):
        units_by_id[ge_id] = unit
        verb = "Applying" if applying else "Previewing"
        print(f"  {verb}: {desc} …", end=" ", flush=True)
        # `resolved` (registry recognition), not recorder presence, is what makes
        # a target real — so an orphan from a prior broken run is never written.
        r = await migrate_entity(
            ws,
            givtcp_id,
            ge_id,
            desc,
            cutover,
            unit,
            applying,
            resolved,
            reset_class,
            tz,
            args.trust_source_sums,
            warn,
        )
        results.append(r)
        print(r.status)
        # Pace writes so the single-threaded recorder can drain between entities.
        if applying and idx < len(plan) - 1:
            await asyncio.sleep(_ENTITY_PAUSE_SECONDS)

    # Mean-series back-port (power / SOC / temperature). Straight mean/min/max
    # copy across the cutover — no sum, no rebase, no plausibility — collected
    # into the same results list so the summary covers both paths.
    if not args.skip_means:
        mean_plan = _build_mean_plan(inv_serials, batt_serials, meta_by_id, canonical_to_actual)
        for idx, (givtcp_id, ge_id, desc, unit, resolved) in enumerate(mean_plan):
            verb = "Applying" if applying else "Previewing"
            print(f"  {verb}: {desc} …", end=" ", flush=True)
            r = await migrate_mean_entity(
                ws, givtcp_id, ge_id, desc, cutover, unit, applying, resolved
            )
            results.append(r)
            print(r.status)
            if applying and idx < len(mean_plan) - 1:
                await asyncio.sleep(_ENTITY_PAUSE_SECONDS)

    summary_code = _print_summary(results, applying)

    # Read-only validation of the resulting sum series, in both dry-run and apply.
    # Residue repair only fires when the user opted in with --apply --repair-residue.
    validation_exit = await run_validation(
        ws,
        results,
        tz,
        units_by_id=units_by_id,
        repair=applying and args.repair_residue,
    )

    await ws.close()

    return max(summary_code, validation_exit)


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
            "dry-run mode and prints a preview without modifying anything."
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
        "--skip-means",
        action="store_true",
        help="Skip back-porting mean-type series (power, SOC, temperatures).",
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
