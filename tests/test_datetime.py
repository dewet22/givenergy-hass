"""Tests for the GivEnergy Local datetime platform (the inverter system clock, #219)."""

from datetime import UTC, datetime

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("datetime", DOMAIN, unique_id)
    assert entity_id is not None, f"No datetime entity for unique_id={unique_id!r}"
    return entity_id


def _maybe_entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("datetime", DOMAIN, unique_id)


async def test_system_time_reads_inverter_clock(hass, setup_integration):
    """The naive inverter clock (fixture 2026-05-10 12:00) is surfaced as a
    timezone-aware value interpreted in HA's configured zone."""
    state = hass.states.get(_entity_id(hass, "SA1234G123_system_time"))
    # HA stores the datetime state as a UTC ISO string; compare instants so the
    # assertion is independent of the test runner's timezone.
    expected = datetime(2026, 5, 10, 12, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    assert dt_util.parse_datetime(state.state) == expected


async def test_set_system_time_writes_local_wall_clock(hass, mock_client, setup_integration):
    """Setting the entity converts HA's tz-aware value to local wall-clock and writes
    the six RTC registers (year stored as year-2000)."""
    entity_id = _entity_id(hass, "SA1234G123_system_time")
    target_utc = datetime(2026, 6, 1, 8, 30, 15, tzinfo=UTC)
    await hass.services.async_call(
        "datetime",
        "set_value",
        {"entity_id": entity_id, "datetime": target_utc.isoformat()},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()
    year, month, day, hour, minute, second = mock_client.one_shot_command.call_args[0][0]
    local = dt_util.as_local(target_utc)
    assert year.value == local.year - 2000
    assert month.value == local.month
    assert day.value == local.day
    assert hour.value == local.hour
    assert minute.value == local.minute
    assert second.value == local.second


async def test_system_time_absent_when_register_none(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """If the clock register reads None on a clean poll, the entity isn't created."""
    mock_inverter.system_time = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _maybe_entity_id(hass, "SA1234G123_system_time") is None
