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

What is migrated by default (all verified ✅ pairs):
  Solar generation today / lifetime
  Grid import today / lifetime
  Grid export today / lifetime
  Battery charge today / discharge today
  Inverter output today / lifetime
  Battery throughput lifetime
  Battery charge cycles (per battery pack)

Opt-in (--include-charge-from-grid):
  Charge from grid lifetime  ⚠️  values differ on some systems — verify manually

Not migrated:
  House load today  ❌  givenergy_local's e_load_day (IR35) reads ~0 on some
    inverters while the GE app's "Consumption today" is correct — splicing it
    produces a cliff at the seam. Excluded pending a library register fix.
  battery_discharge_this_year, work_time_total, total_refresh_failures,
  battery_charge_energy_total_kwh, battery_discharge_energy_total_kwh,
  load_energy_total_kwh  (no GivTCP equivalent or register-level gap)

See docs/migration-from-givtcp.md for the full sensor catalogue and design notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

try:
    import websockets
    import websockets.asyncio.client
except ImportError:
    sys.exit("Missing dependency: pip install 'websockets>=12.0'")


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
    ("invertor_energy_today_kwh", "inverter_output_today", "Inverter output today", True),
    ("invertor_energy_total_kwh", "inverter_output_total", "Inverter output lifetime", True),
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
    # the correct target. Both are PV + grid-in − grid-out − AC-charge, so they
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
# NOTE: GivTCP tracks battery metrics under the inverter serial only — it does
# not create per-battery-pack LTS statistics. The givenergy_local charge_cycles
# sensors (per battery serial) have no GivTCP counterpart to backfill from, so
# this list is intentionally empty.
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
        self._ws = await websockets.asyncio.client.connect(self._url)
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

    async def get_statistics(
        self,
        statistic_ids: list[str],
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        kwargs: dict[str, Any] = {
            "statistic_ids": statistic_ids,
            "start_time": start.isoformat(),
            "period": "hour",
            "types": ["sum", "state"],
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
# Cut-over date detection
# ---------------------------------------------------------------------------


async def detect_cutover(ws: HAWebSocket, inv_sn: str) -> tuple[date | None, date | None]:
    """
    Return (last_givtcp_date, first_ge_date) for the reference inverter sensor.

    Either value may be None if no data was found.
    """
    givtcp_id = f"sensor.givtcp_{inv_sn}_{_CUTOVER_DETECT_GIVTCP}"
    ge_id = f"sensor.givenergy_inverter_{inv_sn}_{_CUTOVER_DETECT_GE}"

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


# ---------------------------------------------------------------------------
# Per-entity migration
# ---------------------------------------------------------------------------


class MigrationResult:
    __slots__ = (
        "description",
        "ge_id",
        "warn_diverged",
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
        self.status = "pending"
        self.givtcp_rows = 0
        self.ge_pre_rows = 0
        self.ge_post_rows = 0
        self.merged_rows = 0
        self.sum_at_cutover: float | None = None
        self.error: str | None = None


_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


async def migrate_entity(
    ws: HAWebSocket,
    givtcp_id: str,
    ge_id: str,
    description: str,
    cutover: datetime,
    ge_unit: str,
    apply: bool,
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

    last_givtcp_sum = givtcp_stats[-1].get("sum") or 0.0
    r.sum_at_cutover = last_givtcp_sum

    rebased_post = rebase_sum(ge_post, last_givtcp_sum)
    merged = givtcp_stats + rebased_post
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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

    # Cut-over detection / validation
    if args.cutover is None:
        print("\nDetecting cut-over date …", end=" ", flush=True)
        last_givtcp, first_ge = await detect_cutover(ws, inv_serials[0])
        print("done")
        print()
        print("  Last GivTCP data point  :", last_givtcp or "not found")
        print("  First givenergy_local   :", first_ge or "not found")
        print()
        if last_givtcp and first_ge:
            # Suggest the later of the two dates — the tail of the overlap
            # window. The boundary is midnight at the *start* of this date,
            # so GivTCP history through the previous day is migrated and GE
            # takes over from 00:00 on the suggested date regardless of when
            # GivTCP actually stopped that day.
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
        await ws.close()
        return 0

    # The boundary is midnight at the *start* of the cutover date. GivTCP history
    # strictly before that moment is migrated; givenergy_local data from that
    # moment onwards is kept. If GivTCP stopped mid-day on the cutover date, GE
    # picks up from 00:00 that day and covers the full day cleanly.
    cutover_date = date.fromisoformat(args.cutover)
    cutover = datetime.combine(cutover_date, datetime.min.time(), tzinfo=UTC)
    print(f"Cut-over date    : {cutover_date} 00:00 UTC")

    if applying:
        print()
        print("  ⚠️  This will CLEAR and REWRITE long-term statistics for the")
        print("  ⚠️  listed givenergy_local entities. Back up your recorder")
        print("  ⚠️  database before proceeding.")
        print("  ℹ️  Re-running (same or different --cutover) is safe: the script")
        print("  ℹ️  always reads the original GivTCP entity for pre-cutover data.")
        try:
            confirm = input("  Type 'yes' to continue: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("Aborted.")
            await ws.close()
            return 1

    print()

    # Build migration plan
    plan: list[tuple[str, str, str, str, bool]] = []

    for sn in inv_serials:
        for gt_sfx, ge_sfx, desc, default in INVERTER_PAIRS:
            if not default and not args.include_charge_from_grid:
                continue
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            ge_id = f"sensor.givenergy_inverter_{sn}_{ge_sfx}"
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or "kWh"
            plan.append((givtcp_id, ge_id, desc, unit, not default))

    for sn in batt_serials:
        for gt_sfx, ge_sfx, desc, fallback_unit in BATTERY_PAIRS:
            givtcp_id = f"sensor.givtcp_{sn}_{gt_sfx}"
            ge_id = f"sensor.givenergy_battery_{sn}_{ge_sfx}"
            unit = meta_by_id.get(ge_id, {}).get("unit_of_measurement") or fallback_unit
            plan.append((givtcp_id, ge_id, desc, unit, False))

    # Execute
    results: list[MigrationResult] = []
    for idx, (givtcp_id, ge_id, desc, unit, warn) in enumerate(plan):
        verb = "Applying" if applying else "Previewing"
        print(f"  {verb}: {desc} …", end=" ", flush=True)
        r = await migrate_entity(ws, givtcp_id, ge_id, desc, cutover, unit, applying, warn)
        results.append(r)
        print(r.status)
        # Pace writes so the single-threaded recorder can drain between entities.
        if applying and idx < len(plan) - 1:
            await asyncio.sleep(_ENTITY_PAUSE_SECONDS)

    await ws.close()

    # Summary table
    W = 46
    print()
    print("─" * 88)
    print(f"  {'Sensor':<{W}} {'GivTCP':>8} {'GE pre':>7} {'GE post':>8}  Result")
    print("─" * 88)
    for r in results:
        warn_tag = (
            "  ⚠️  verify values" if r.warn_diverged and r.status in ("migrated", "dry_run") else ""
        )
        print(
            f"  {r.description:<{W}} {r.givtcp_rows:>8} {r.ge_pre_rows:>7}"
            f" {r.ge_post_rows:>8}  {r.status}{warn_tag}"
        )
    print("─" * 88)

    migrated = sum(1 for r in results if r.status == "migrated")
    planned = sum(1 for r in results if r.status == "dry_run")
    skipped = sum(1 for r in results if r.status == "no_givtcp_data")
    errored = sum(1 for r in results if r.status == "error")

    if not applying:
        print(f"\n  Planned: {planned}  |  No GivTCP data: {skipped}  |  Errors: {errored}")
        print("  Add --apply to write changes (back up your DB first).")
    else:
        print(f"\n  Migrated: {migrated}  |  No GivTCP data: {skipped}  |  Errors: {errored}")

    for r in results:
        if r.status == "error":
            print(f"\n  ERROR — {r.description}: {r.error}", file=sys.stderr)

    return 0 if errored == 0 else 2


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
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
