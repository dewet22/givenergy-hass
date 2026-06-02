"""Tests for integration setup, unload, and config-entry migration."""

from unittest.mock import AsyncMock, MagicMock, patch

from givenergy_modbus.exceptions import PlantTopologyMismatch
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local import (
    _CARD_URL,
    _CARD_VERSION,
    _missing_dashboard_cards,
    async_setup,
)
from custom_components.givenergy_local.const import (
    CONF_RETRIES,
    CONF_TIMEOUT_TOLERANCE,
    DOMAIN,
    EXPOSE_RECOMMENDED_ENTITY_KEYS,
    SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
    SERVICE_REDETECT_PLANT,
    SERVICE_SET_SYSTEM_DATETIME,
)


def _hass_with_resources(urls: list[str]) -> MagicMock:
    hass = MagicMock()
    resources = MagicMock()
    resources.async_items = MagicMock(return_value=[{"url": u} for u in urls])
    hass.data = {"lovelace": MagicMock(resources=resources)}
    return hass


async def test_missing_dashboard_cards_flags_absent_only():
    hass = _hass_with_resources(["/hacsfiles/apexcharts-card/apexcharts-card.js"])
    assert await _missing_dashboard_cards(hass) == ["power-flow-card-plus"]


async def test_missing_dashboard_cards_empty_when_all_present():
    hass = _hass_with_resources(
        [
            "/hacsfiles/apexcharts-card/apexcharts-card.js",
            "/hacsfiles/power-flow-card-plus/power-flow-card-plus.js",
        ]
    )
    assert await _missing_dashboard_cards(hass) == []


async def test_missing_dashboard_cards_silent_when_registry_absent():
    hass = MagicMock()
    hass.data = {}  # no lovelace data at all
    assert await _missing_dashboard_cards(hass) == []


async def test_missing_dashboard_cards_swallows_registry_errors():
    hass = MagicMock()
    resources = MagicMock()
    resources.async_items = MagicMock(side_effect=RuntimeError("boom"))
    hass.data = {"lovelace": MagicMock(resources=resources)}
    assert await _missing_dashboard_cards(hass) == []


async def test_frontend_card_served_and_autoloaded():
    """The bundled heatmap card is served + auto-loaded at component setup."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()

    with patch("custom_components.givenergy_local.add_extra_js_url") as add_js:
        assert await async_setup(hass, {}) is True

    hass.http.async_register_static_paths.assert_awaited_once()
    (paths,) = hass.http.async_register_static_paths.call_args[0]
    assert paths[0].url_path == _CARD_URL
    add_js.assert_called_once_with(hass, f"{_CARD_URL}?v={_CARD_VERSION}")


async def test_frontend_card_registered_once_across_multiple_entries(hass, mock_client):
    """With several config entries (multi-inverter / EMS), the card still registers
    exactly once — it lives at component scope (async_setup), not per entry, so the
    entries can't race on the shared static path (regression for hass#52). Under the
    old per-entry registration this fires once per entry."""
    entries = [
        MockConfigEntry(
            domain=DOMAIN,
            data={"host": f"192.168.1.{n}", "port": 8899, "scan_interval": 30},
            unique_id=f"entry-{n}",
        )
        for n in (10, 11, 12)
    ]
    with patch("custom_components.givenergy_local._async_register_frontend_card") as mock_register:
        for entry in entries:
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    mock_register.assert_called_once()


async def test_frontend_card_skipped_when_http_unavailable():
    """No http server (minimal env / tests) -> skip cleanly, never raise."""
    hass = MagicMock()
    hass.data = {}
    hass.http = None

    with patch("custom_components.givenergy_local.add_extra_js_url") as add_js:
        assert await async_setup(hass, {}) is True

    add_js.assert_not_called()


async def test_frontend_card_failure_does_not_break_setup():
    """A registration error must be swallowed so component setup still succeeds."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("custom_components.givenergy_local.add_extra_js_url") as add_js:
        assert await async_setup(hass, {}) is True  # must not raise

    add_js.assert_not_called()


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


async def test_set_system_datetime_service_sends_command(hass, mock_client, setup_integration):
    """The set_system_datetime service writes the inverter clock for the device."""
    device_reg = dr.async_get(hass)
    inverter_device = next(
        d for d in device_reg.devices.values() if setup_integration.entry_id in d.config_entries
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SYSTEM_DATETIME,
        {"device_id": inverter_device.id},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


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


async def test_cold_start_skips_capabilities_persist_on_partial_seed(
    hass, mock_client, mock_config_entry
):
    """A partial cold seed still loads the integration (the inverter identified
    itself), but its possibly-degraded topology must NOT be persisted — otherwise
    flaky kit could vanish permanently on the next warm start."""
    from givenergy_modbus.exceptions import ReadFailure, RefreshPartiallySucceeded

    failure = ReadFailure(
        device_address=0x31,
        request_type="ReadHoldingRegisters",
        base_register=300,
        register_count=60,
    )
    mock_client.refresh = AsyncMock(
        side_effect=RefreshPartiallySucceeded(
            "partial seed",
            plant=mock_client.plant,
            failures=[failure],
            cause=ExceptionGroup("reads", [TimeoutError()]),
        )
    )
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

    # Loaded (served the partial), but topology deliberately not committed.
    assert mock_config_entry.state is ConfigEntryState.LOADED
    save_mock.assert_not_awaited()


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
# Voice-assistant exposure (issue #65)
# ---------------------------------------------------------------------------


async def test_expose_recommended_entities_service(hass, mock_client, setup_integration):
    """The service walks the entry's registered entities, matches the curated key
    set against unique_id suffixes, and calls async_expose_entity for each."""
    device_reg = dr.async_get(hass)
    inverter_device = next(
        d for d in device_reg.devices.values() if setup_integration.entry_id in d.config_entries
    )

    with patch("custom_components.givenergy_local.async_expose_entity") as expose_mock:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
            {"device_id": inverter_device.id},
            blocking=True,
        )

    exposed_entity_ids = {call.args[2] for call in expose_mock.call_args_list}

    # Look entities up by their unique_id (which IS `{serial}_{description.key}`,
    # so it tracks the curated key list directly) and assert their entity_ids
    # are in the exposed set. Catches the class of bug where a curated key
    # silently doesn't match any entity — e.g. listing the entity's
    # translation_key instead of its description key.
    entity_reg = er.async_get(hass)
    inverter_serial = "SA1234G123"  # from mock_config_entry fixture
    for key in ("p_pv", "battery_soc", "p_grid_out", "p_load_demand", "status"):
        entry = entity_reg.async_get_entity_id("sensor", DOMAIN, f"{inverter_serial}_{key}")
        assert entry is not None, (
            f"No entity registered with unique_id {inverter_serial}_{key!r} — "
            "curated key may be a translation_key rather than the description key"
        )
        assert entry in exposed_entity_ids, f"Entity {entry} was registered but not exposed"

    # All of the curated, enabled keys present as entities should be exposed;
    # nothing outside the set or disabled should be.
    for entry in er.async_entries_for_config_entry(entity_reg, setup_integration.entry_id):
        if entry.disabled_by is not None:
            assert entry.entity_id not in exposed_entity_ids, (
                f"Disabled entity {entry.entity_id} should not have been exposed"
            )
            continue
        in_curated = any(entry.unique_id.endswith(f"_{k}") for k in EXPOSE_RECOMMENDED_ENTITY_KEYS)
        assert (entry.entity_id in exposed_entity_ids) == in_curated, (
            f"Entity {entry.entity_id} (unique_id={entry.unique_id}) "
            f"{'should' if in_curated else 'should not'} be exposed"
        )

    # All exposure calls targeted the default "conversation" assistant.
    for call in expose_mock.call_args_list:
        assert call.args[1] == "conversation"
        assert call.args[3] is True


async def test_expose_recommended_entities_honours_custom_assistants_list(
    hass, mock_client, setup_integration
):
    """The `assistants` parameter routes a single exposure call per (entity, assistant)."""
    device_reg = dr.async_get(hass)
    inverter_device = next(
        d for d in device_reg.devices.values() if setup_integration.entry_id in d.config_entries
    )

    with patch("custom_components.givenergy_local.async_expose_entity") as expose_mock:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
            {
                "device_id": inverter_device.id,
                "assistants": ["conversation", "cloud.alexa"],
            },
            blocking=True,
        )

    # Two assistants × N entities → 2N total exposure calls.
    assistants_called = {call.args[1] for call in expose_mock.call_args_list}
    assert assistants_called == {"conversation", "cloud.alexa"}
