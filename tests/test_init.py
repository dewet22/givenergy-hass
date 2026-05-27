"""Tests for integration setup, unload, and config-entry migration."""

from unittest.mock import AsyncMock, patch

from givenergy_modbus.exceptions import PlantTopologyMismatch
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local.const import (
    CONF_RETRIES,
    CONF_TIMEOUT_TOLERANCE,
    DOMAIN,
    SERVICE_REDETECT_PLANT,
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


# ---------------------------------------------------------------------------
# PlantCapabilities cache integration (issue #48)
# ---------------------------------------------------------------------------


async def test_redetect_plant_service_clears_cache_and_reloads(
    hass, mock_client, setup_integration
):
    """The redetect_plant service removes the per-entry Store file and schedules a reload."""
    # The integration registers an inverter device whose only config_entry is
    # setup_integration.entry_id — that's the linkage the service walks.
    device_reg = dr.async_get(hass)
    inverter_device = next(
        d for d in device_reg.devices.values() if setup_integration.entry_id in d.config_entries
    )

    fake_store = AsyncMock()
    with (
        patch(
            "custom_components.givenergy_local._capabilities_store", return_value=fake_store
        ) as store_factory,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ) as reload_mock,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REDETECT_PLANT,
            {"device_id": inverter_device.id},
            blocking=True,
        )
        await hass.async_block_till_done()

    store_factory.assert_called_with(hass, setup_integration.entry_id)
    fake_store.async_remove.assert_awaited_once()
    reload_mock.assert_called_with(setup_integration.entry_id)


async def test_topology_mismatch_persists_actual_and_raises_repairs_issue(
    hass, mock_client, mock_config_entry
):
    """End-to-end: detect(prior=) raising PlantTopologyMismatch persists the
    new layout, raises an advisory Repairs issue, and queues a reload.
    """
    actual = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32, 0x33],
        bcu_stacks=[],
    )
    mismatch = PlantTopologyMismatch(
        "topology changed",
        prior=mock_client.plant.capabilities,
        actual=actual,
    )
    mock_client.detect = AsyncMock(side_effect=mismatch)

    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.givenergy_local._load_capabilities",
            new=AsyncMock(return_value=mock_client.plant.capabilities),
        ),
        patch("custom_components.givenergy_local._save_capabilities", new=AsyncMock()) as save_mock,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ) as reload_mock,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # The callback persisted the actual topology. (Save-on-success doesn't fire
    # on the warm-hit-then-mismatch path because prior_capabilities was not None.)
    save_mock.assert_awaited_with(hass, mock_config_entry.entry_id, actual)
    # An advisory Repairs issue was raised for this entry.
    issues = ir.async_get(hass).issues
    assert (DOMAIN, f"plant_topology_changed_{mock_config_entry.entry_id}") in issues
    # And a reload was queued.
    reload_mock.assert_called_with(mock_config_entry.entry_id)


async def test_cold_start_saves_capabilities_on_first_successful_refresh(
    hass, mock_client, mock_config_entry
):
    """On a cold start (no cache, no mismatch), save fires once with the
    confirmed live capabilities so the next restart is a warm start."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.givenergy_local._load_capabilities",
            new=AsyncMock(return_value=None),
        ),
        patch("custom_components.givenergy_local._save_capabilities", new=AsyncMock()) as save_mock,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    mock_client.detect.assert_awaited_once_with(prior=None)
    save_mock.assert_awaited_with(hass, mock_config_entry.entry_id, mock_client.plant.capabilities)


# ---------------------------------------------------------------------------
# Capabilities Store helper unit tests
# ---------------------------------------------------------------------------


def _sample_capabilities() -> PlantCapabilities:
    return PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )


async def test_load_capabilities_returns_none_on_cache_miss(hass):
    """A missing on-disk file returns None — caller will run a cold detect()."""
    from custom_components.givenergy_local import _load_capabilities

    fake_store = AsyncMock()
    fake_store.async_load = AsyncMock(return_value=None)
    with patch("custom_components.givenergy_local._capabilities_store", return_value=fake_store):
        result = await _load_capabilities(hass, "entry_id")
    assert result is None


async def test_load_capabilities_absorbs_library_rejection(hass):
    """A payload the library can't decode (corrupt or library-schema-bumped) is a miss."""
    from custom_components.givenergy_local import _load_capabilities

    fake_store = AsyncMock()
    fake_store.async_load = AsyncMock(return_value={"schema_version": 99})
    with patch("custom_components.givenergy_local._capabilities_store", return_value=fake_store):
        result = await _load_capabilities(hass, "entry_id")
    assert result is None


async def test_load_capabilities_absorbs_typeerror_from_hand_edited_payload(hass):
    """A hand-edited cache file with non-tuple bcu_stacks trips library TypeError.
    The helper must absorb it as a miss rather than crashing setup."""
    from custom_components.givenergy_local import _load_capabilities

    broken = _sample_capabilities().to_dict()
    broken["bcu_stacks"] = [42]  # bare int → TypeError on tuple unpack

    fake_store = AsyncMock()
    fake_store.async_load = AsyncMock(return_value=broken)
    with patch("custom_components.givenergy_local._capabilities_store", return_value=fake_store):
        result = await _load_capabilities(hass, "entry_id")
    assert result is None


async def test_save_capabilities_writes_library_dict_directly(hass):
    """save writes capabilities.to_dict() verbatim — no envelope."""
    from custom_components.givenergy_local import _save_capabilities

    caps = _sample_capabilities()
    fake_store = AsyncMock()
    with patch("custom_components.givenergy_local._capabilities_store", return_value=fake_store):
        await _save_capabilities(hass, "entry_id", caps)
    fake_store.async_save.assert_awaited_once_with(caps.to_dict())


# ---------------------------------------------------------------------------
# Real-Store integration tests — no helper patching. Exercise the actual
# HA Store machinery through pytest-homeassistant-custom-component's mock
# filesystem to cover the warm-start and topology-mismatch lifecycles end
# to end.
# ---------------------------------------------------------------------------


async def test_warm_start_cycle_uses_real_store_to_seed_prior(hass, mock_client, mock_config_entry):
    """Cold setup writes capabilities through the real Store. Reloading the
    entry then reads them back and threads them into Client.detect(prior=)."""
    mock_config_entry.add_to_hass(hass)

    # ----- Cold start: no prior file exists yet. -----
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    cold_detect_calls = mock_client.detect.await_args_list
    assert len(cold_detect_calls) == 1
    assert cold_detect_calls[0].kwargs == {"prior": None}

    # ----- Reload: the file written during cold start must be read back as prior. -----
    mock_client.detect.reset_mock()
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    warm_detect_calls = mock_client.detect.await_args_list
    assert len(warm_detect_calls) == 1
    prior = warm_detect_calls[0].kwargs["prior"]
    assert prior is not None
    # PlantCapabilities round-trips by-value via Pydantic — compare via to_dict().
    assert prior.to_dict() == mock_client.plant.capabilities.to_dict()


async def test_topology_mismatch_reload_cycle_uses_actual_on_next_setup(
    hass, mock_client, mock_config_entry
):
    """Pre-seed the Store with a prior. Setup detects a mismatch, persists
    the new actual via the callback, schedules a reload. On the next
    setup_entry pass the reload reads back `actual` and threads it as prior."""
    from custom_components.givenergy_local import _save_capabilities

    prior_caps = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    actual_caps = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32, 0x33],  # one extra battery
        bcu_stacks=[],
    )
    mock_config_entry.add_to_hass(hass)

    # Seed the real Store before setup — simulates a previous cold-start boot
    # that recorded prior_caps.
    await _save_capabilities(hass, mock_config_entry.entry_id, prior_caps)

    # First detect() raises mismatch; the reload's setup_entry calls detect
    # again and we want that one to succeed cleanly so the cycle terminates.
    mock_client.detect.side_effect = [
        PlantTopologyMismatch("topology changed", prior=prior_caps, actual=actual_caps),
        None,
    ]

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()  # drains the scheduled reload task

    calls = mock_client.detect.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["prior"].to_dict() == prior_caps.to_dict()
    assert calls[1].kwargs["prior"].to_dict() == actual_caps.to_dict()

    issues = ir.async_get(hass).issues
    assert (DOMAIN, f"plant_topology_changed_{mock_config_entry.entry_id}") in issues


async def test_redetect_service_raises_on_unknown_device(hass, mock_client, setup_integration):
    """An unknown device_id raises HomeAssistantError rather than silently noop'ing."""
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="No GivEnergy device"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REDETECT_PLANT,
            {"device_id": "definitely-not-a-real-device-id"},
            blocking=True,
        )


async def test_redetect_service_raises_when_device_has_no_matching_entry(
    hass, mock_client, setup_integration
):
    """A device whose config entries are all foreign to this integration
    raises HomeAssistantError rather than picking an arbitrary entry."""
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    # Set up a second entry from a different domain, then register a device
    # under it. The redetect service should refuse to act on that device.
    foreign_entry = MockConfigEntry(domain="some_other_integration", data={})
    foreign_entry.add_to_hass(hass)
    device_reg = dr.async_get(hass)
    foreign_device = device_reg.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("some_other_integration", "foreign")},
    )

    with pytest.raises(HomeAssistantError, match="No GivEnergy config entry"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REDETECT_PLANT,
            {"device_id": foreign_device.id},
            blocking=True,
        )


async def test_redetect_service_triggers_cold_reload_via_real_store(
    hass, mock_client, setup_integration
):
    """The redetect_plant service removes the per-entry Store file and reloads.
    The post-reload setup_entry observes no prior (cold detect) — which is
    only true if the file was really removed before the reload's load() ran."""
    from custom_components.givenergy_local import _load_capabilities

    # Cold setup_integration populated the Store on the way in.
    assert await _load_capabilities(hass, setup_integration.entry_id) is not None

    device_reg = dr.async_get(hass)
    inverter_device = next(
        d for d in device_reg.devices.values() if setup_integration.entry_id in d.config_entries
    )

    # Reset detect's call history so we observe only the reload's call.
    mock_client.detect.reset_mock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REDETECT_PLANT,
        {"device_id": inverter_device.id},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The reload re-ran setup_entry. Because the service removed the Store
    # file first, the post-reload load() returned None, so detect() was called
    # with prior=None (the cold-detect semantic the service is meant to provide).
    reload_calls = mock_client.detect.await_args_list
    assert len(reload_calls) >= 1
    assert reload_calls[-1].kwargs == {"prior": None}
