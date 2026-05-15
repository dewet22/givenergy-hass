"""Tests for integration setup, unload, and config-entry migration."""

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local.const import (
    CONF_RETRIES,
    CONF_TIMEOUT_TOLERANCE,
    DOMAIN,
)


async def test_migrate_v1_entry_strips_retries_and_tolerance(hass, mock_client):
    """A pre-v2 entry that stored retries/tolerance has those fields dropped
    on migration; the version bumps to 2; setup proceeds normally."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "host": "192.168.1.100",
            "port": 8899,
            "scan_interval": 30,
            "passive": False,
            CONF_TIMEOUT_TOLERANCE: 7,  # user had a custom override
            CONF_RETRIES: 3,  # user had a custom override
        },
        unique_id="SA1234G123",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert CONF_TIMEOUT_TOLERANCE not in entry.data
    assert CONF_RETRIES not in entry.data
    # Untouched fields survive.
    assert entry.data["host"] == "192.168.1.100"
    assert entry.data["scan_interval"] == 30


async def test_migrate_v1_entry_without_legacy_fields_is_idempotent(hass, mock_client):
    """A v1 entry that never had the legacy fields still migrates cleanly to v2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "host": "192.168.1.100",
            "port": 8899,
            "scan_interval": 30,
            "passive": False,
        },
        unique_id="SA1234G123",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2


async def test_migrate_refuses_future_version(hass, mock_client):
    """A config entry from a future schema version should fail migration
    rather than silently downgrade."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=99,
        data={"host": "192.168.1.100", "port": 8899},
        unique_id="SA1234G123",
    )
    entry.add_to_hass(hass)

    # Setup is expected to fail; HA marks the entry as migration_error.
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
