"""Tests for the migration script's I/O layer and timestamp/helper utilities.

The stats-repair analysis functions are covered in test_migrate_stats_repair.py;
this file targets the previously-untested glue: the timestamp normalisers, the
sum rebaser, the cutover-suggestion printer, and the HAWebSocket client's
request/response, retry, and chunking logic (driven by an in-memory fake socket
so no real network is involved).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

_MIGRATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_from_givtcp.py"


def _load_migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_from_givtcp", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_migrate_module()


# ---------------------------------------------------------------------------
# Timestamp helpers: _to_utc / _as_iso / _normalise
# ---------------------------------------------------------------------------


def test_to_utc_unix_seconds_and_milliseconds_agree():
    """A Unix timestamp is accepted in seconds or HA's modern milliseconds; the
    >1e11 heuristic divides ms down so both resolve to the same instant."""
    secs = _MOD._to_utc(1_700_000_000)
    millis = _MOD._to_utc(1_700_000_000_000)
    assert secs == millis == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert secs.tzinfo is UTC


def test_to_utc_parses_iso_strings_to_utc():
    """ISO strings — Z suffix or a non-UTC offset — are normalised to UTC."""
    assert _MOD._to_utc("2026-05-20T08:00:00+00:00") == datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
    assert _MOD._to_utc("2026-05-20T08:00:00Z") == datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
    # A +01:00 offset is one hour ahead of UTC.
    assert _MOD._to_utc("2026-05-20T09:00:00+01:00") == datetime(2026, 5, 20, 8, 0, tzinfo=UTC)


def test_as_iso_renders_in_utc():
    """A non-UTC datetime is shifted to UTC before serialising."""
    london_summer = datetime(2026, 6, 20, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # BST = +01:00
    assert _MOD._as_iso(london_summer) == "2026-06-20T08:00:00+00:00"
    fixed = datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    assert _MOD._as_iso(fixed) == "2025-12-31T19:00:00+00:00"


def test_normalise_strips_end_and_canonicalises_start():
    """_normalise drops `end`, rewrites `start` as a UTC ISO string, and copies
    (the input row is left untouched)."""
    row = {"start": "2026-05-20T09:00:00+01:00", "end": "2026-05-20T10:00:00+01:00", "state": 5.0}
    out = _MOD._normalise(row)
    assert out == {"start": "2026-05-20T08:00:00+00:00", "state": 5.0}
    assert "end" not in out
    # Original is not mutated.
    assert row["end"] == "2026-05-20T10:00:00+01:00"
    assert row["start"] == "2026-05-20T09:00:00+01:00"


# ---------------------------------------------------------------------------
# rebase_sum
# ---------------------------------------------------------------------------


def test_rebase_sum_empty_returns_empty():
    assert _MOD.rebase_sum([], 100.0) == []


def test_rebase_sum_shifts_preserving_deltas():
    """Every `sum` shifts by the same offset so the curve's shape is unchanged."""
    stats = [{"start": "a", "sum": 10.0}, {"start": "b", "sum": 12.5}, {"start": "c", "sum": 13.0}]
    out = _MOD.rebase_sum(stats, 100.0)
    assert [r["sum"] for r in out] == [100.0, 102.5, 103.0]
    # Inputs are copied, not mutated.
    assert stats[0]["sum"] == 10.0


def test_rebase_sum_near_zero_offset_returns_copies():
    """When the first sum already equals the base, rows are returned as copies
    (same values, distinct dicts)."""
    stats = [{"start": "a", "sum": 100.0}, {"start": "b", "sum": 101.0}]
    out = _MOD.rebase_sum(stats, 100.0)
    assert [r["sum"] for r in out] == [100.0, 101.0]
    assert out[0] is not stats[0]


def test_rebase_sum_preserves_none_sums():
    """A row with no `sum` (a gap) is carried through without becoming a number."""
    stats = [{"start": "a", "sum": 5.0}, {"start": "b", "sum": None}, {"start": "c", "sum": 7.0}]
    out = _MOD.rebase_sum(stats, 10.0)
    assert [r["sum"] for r in out] == [10.0, None, 12.0]


# ---------------------------------------------------------------------------
# _state_deltas
# ---------------------------------------------------------------------------


def test_state_deltas_empty_and_single_row():
    assert _MOD._state_deltas([]) == []
    assert _MOD._state_deltas([{"state": 1.0}]) == []


def test_state_deltas_includes_negatives_and_skips_none():
    """Consecutive differences are taken across present readings only; a None
    reading drops both deltas that would touch it, and resets (negatives) stay."""
    rows = [{"state": 1.0}, {"state": 3.0}, {"state": None}, {"state": 4.0}, {"state": 2.0}]
    # 3-1=2 (kept); pairs touching the None are skipped; 2-4=-2 (kept).
    assert _MOD._state_deltas(rows) == [2.0, -2.0]


# ---------------------------------------------------------------------------
# _print_cutover_suggestion
# ---------------------------------------------------------------------------


def test_print_cutover_suggestion_both_dates(capsys):
    """With both boundaries, it suggests the later date and explains the split."""
    _MOD._print_cutover_suggestion(date(2026, 5, 18), date(2026, 5, 20))
    out = capsys.readouterr().out
    assert "Suggested --cutover date: 2026-05-20" in out
    assert "through 2026-05-19 23:59 UTC" in out  # previous day migrated
    assert "from 2026-05-20 00:00 UTC" in out


def test_print_cutover_suggestion_only_givtcp(capsys):
    """GivTCP data but no GE data yet → advise installing and waiting a day."""
    _MOD._print_cutover_suggestion(date(2026, 5, 18), None)
    out = capsys.readouterr().out
    assert "no statistics yet" in out
    assert "Suggested --cutover" not in out


def test_print_cutover_suggestion_neither(capsys):
    """No GivTCP data at all → nothing to migrate."""
    _MOD._print_cutover_suggestion(None, None)
    out = capsys.readouterr().out
    assert "not found" in out
    assert "Nothing to migrate" in out


# ---------------------------------------------------------------------------
# HAWebSocket: construction + request/response + retry + chunking
# ---------------------------------------------------------------------------


class _FakeWS:
    """In-memory stand-in for a websockets connection: records sent frames and
    replays a queued list of response dicts."""

    def __init__(self, responses: list[dict]) -> None:
        self.sent: list[dict] = []
        self._responses = list(responses)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return json.dumps(self._responses.pop(0))

    async def close(self) -> None:  # pragma: no cover - parity with the real client
        pass


def test_haweb_socket_init_normalises_url_and_scheme():
    assert (
        _MOD.HAWebSocket("http://localhost:8123", "tok")._url == "ws://localhost:8123/api/websocket"
    )
    assert _MOD.HAWebSocket("https://ha.example", "tok")._url == "wss://ha.example/api/websocket"
    # A trailing slash on the base URL doesn't double up.
    ws = _MOD.HAWebSocket("http://localhost:8123/", "secret")
    assert ws._url == "ws://localhost:8123/api/websocket"
    assert ws._token == "secret"
    assert ws._msg_id == 0


async def test_call_sends_framed_request_and_returns_result():
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._ws = _FakeWS([{"id": 1, "type": "result", "success": True, "result": {"ok": 1}}])
    result = await ws._call("get_config", foo="bar")
    assert result == {"ok": 1}
    # The outgoing frame carries an incrementing id and the kwargs.
    assert ws._ws.sent[0] == {"type": "get_config", "id": 1, "foo": "bar"}
    assert ws._msg_id == 1


async def test_call_ignores_messages_with_other_ids():
    """Interleaved events / responses to other requests are skipped until the
    matching id arrives."""
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._ws = _FakeWS(
        [
            {"id": 999, "type": "event"},  # unrelated, skipped
            {"id": 1, "success": True, "result": 42},
        ]
    )
    assert await ws._call("recorder/list_statistic_ids") == 42


async def test_call_raises_on_error_response():
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._ws = _FakeWS([{"id": 1, "success": False, "error": {"code": "bad"}}])
    with pytest.raises(RuntimeError, match="get_config"):
        await ws._call("get_config")


async def test_call_with_retry_retries_on_timeout_then_succeeds():
    """A 'timeout' error is retried (recorder commands are idempotent); a later
    attempt's success is returned."""
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._call = AsyncMock(
        side_effect=[
            RuntimeError("HA returned an error: {'code': 'timeout'}"),
            RuntimeError("timeout again"),
            "done",
        ]
    )
    with patch.object(_MOD.asyncio, "sleep", AsyncMock()) as sleep:
        result = await ws._call_with_retry("recorder/clear_statistics", statistic_ids=[])
    assert result == "done"
    assert ws._call.await_count == 3
    assert sleep.await_count == 2  # backoff between the three attempts


async def test_call_with_retry_reraises_non_timeout_immediately():
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._call = AsyncMock(side_effect=RuntimeError("permission denied"))
    with patch.object(_MOD.asyncio, "sleep", AsyncMock()):
        with pytest.raises(RuntimeError, match="permission denied"):
            await ws._call_with_retry("recorder/import_statistics")
    ws._call.assert_awaited_once()  # not retried


async def test_import_statistics_single_chunk():
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._call_with_retry = AsyncMock()
    stats = [{"start": i} for i in range(3)]
    await ws.import_statistics({"statistic_id": "x"}, stats)
    ws._call_with_retry.assert_awaited_once()
    assert ws._call_with_retry.await_args.kwargs["stats"] == stats


async def test_import_statistics_splits_into_chunks():
    """Imports larger than _IMPORT_CHUNK_ROWS are split so a single call can't
    monopolise the recorder thread; chunks tile the input exactly."""
    ws = _MOD.HAWebSocket("http://h:8123", "t")
    ws._call_with_retry = AsyncMock()
    chunk = _MOD._IMPORT_CHUNK_ROWS
    stats = [{"start": i} for i in range(chunk + 5)]
    await ws.import_statistics({"statistic_id": "x"}, stats)
    sizes = [len(c.kwargs["stats"]) for c in ws._call_with_retry.await_args_list]
    assert sizes == [chunk, 5]
    # Same metadata rides every chunk, and the chunks reassemble the input.
    calls = ws._call_with_retry.await_args_list
    assert all(c.kwargs["metadata"] == {"statistic_id": "x"} for c in calls)
    reassembled = [r for c in calls for r in c.kwargs["stats"]]
    assert reassembled == stats
