"""Tests for integration setup, unload, and config-entry migration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from givenergy_modbus.exceptions import PlantTopologyMismatch
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local import (
    _STRATEGY_URL,
    _STRATEGY_VERSION,
    async_setup,
)
from custom_components.givenergy_local.const import (
    CONF_RETRIES,
    CONF_TIMEOUT_TOLERANCE,
    DOMAIN,
    EXPOSE_RECOMMENDED_ENTITY_KEYS,
    SERVICE_CALIBRATE_BATTERY_SOC,
    SERVICE_CAPTURE_FRAMES,
    SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
    SERVICE_REBOOT_INVERTER,
    SERVICE_REDETECT_PLANT,
    SERVICE_SET_SYSTEM_DATETIME,
)


async def test_frontend_modules_served_and_autoloaded():
    """The bundled frontend module (strategy + heatmap card) is served +
    auto-loaded at component setup."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()

    with (
        patch("custom_components.givenergy_local.add_extra_js_url") as add_js,
        patch("custom_components.givenergy_local._async_register_capture_http"),
    ):
        assert await async_setup(hass, {}) is True

    hass.http.async_register_static_paths.assert_awaited_once()
    served_url = hass.http.async_register_static_paths.call_args.args[0][0].url_path
    assert served_url == _STRATEGY_URL
    add_js.assert_called_once_with(hass, f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}")


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

    with (
        patch("custom_components.givenergy_local.add_extra_js_url") as add_js,
        patch("custom_components.givenergy_local._async_register_capture_http"),
    ):
        assert await async_setup(hass, {}) is True

    add_js.assert_not_called()


async def test_frontend_card_failure_does_not_break_setup():
    """A registration error must be swallowed so component setup still succeeds."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch("custom_components.givenergy_local.add_extra_js_url") as add_js,
        patch("custom_components.givenergy_local._async_register_capture_http"),
    ):
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


def _inverter_device_id(hass, entry_id):
    """Return the HA device_id of the inverter registered for `entry_id`."""
    device_reg = dr.async_get(hass)
    return next(d for d in device_reg.devices.values() if entry_id in d.config_entries).id


async def test_reboot_inverter_service_sends_command(hass, mock_client, setup_integration):
    """The reboot_inverter service issues a one-shot reboot command for the device."""
    device_id = _inverter_device_id(hass, setup_integration.entry_id)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REBOOT_INVERTER,
        {"device_id": device_id},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


async def test_calibrate_battery_soc_service_sends_command(hass, mock_client, setup_integration):
    """The calibrate_battery_soc service issues a one-shot calibration command."""
    device_id = _inverter_device_id(hass, setup_integration.entry_id)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CALIBRATE_BATTERY_SOC,
        {"device_id": device_id},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


@pytest.mark.parametrize(
    "service",
    [SERVICE_REBOOT_INVERTER, SERVICE_CALIBRATE_BATTERY_SOC, SERVICE_SET_SYSTEM_DATETIME],
)
async def test_device_services_reject_unknown_device(hass, mock_client, setup_integration, service):
    """Hardware-command services raise (and send nothing) for an unknown device_id."""
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": "does-not-exist"},
            blocking=True,
        )
    mock_client.one_shot_command.assert_not_called()


@pytest.mark.parametrize(
    "service",
    [SERVICE_REBOOT_INVERTER, SERVICE_CALIBRATE_BATTERY_SOC, SERVICE_SET_SYSTEM_DATETIME],
)
async def test_device_services_reject_disconnected_client(
    hass, mock_client, setup_integration, service
):
    """Hardware-command services refuse to act while the inverter is offline."""
    device_id = _inverter_device_id(hass, setup_integration.entry_id)
    mock_client.connected = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": device_id},
            blocking=True,
        )
    mock_client.one_shot_command.assert_not_called()


async def test_set_system_datetime_resolves_by_serial(hass, mock_client, setup_integration):
    """set_system_datetime also accepts a `serial` target (the unique_id path)."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SYSTEM_DATETIME,
        {"serial": setup_integration.unique_id},
        blocking=True,
    )
    mock_client.one_shot_command.assert_called_once()


async def test_set_system_datetime_unknown_serial_raises(hass, mock_client, setup_integration):
    """An unmatched serial surfaces a HomeAssistantError and sends nothing."""
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SYSTEM_DATETIME,
            {"serial": "NOPE0000X1"},
            blocking=True,
        )
    mock_client.one_shot_command.assert_not_called()


async def test_capture_frames_unknown_device_raises(hass, mock_client, setup_integration):
    """capture_frames rejects an unknown device_id before touching the wire."""
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CAPTURE_FRAMES,
            {"device_id": "does-not-exist", "duration": 10},
            blocking=True,
        )
    mock_client.capture_frames.assert_not_called()


async def test_capture_frames_no_connected_inverter_raises(hass, mock_client, setup_integration):
    """With no device_id and every inverter offline, capture_frames raises."""
    mock_client.connected = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CAPTURE_FRAMES,
            {"duration": 10},
            blocking=True,
        )
    mock_client.capture_frames.assert_not_called()


async def test_expose_recommended_entities_unknown_device_raises(
    hass, mock_client, setup_integration
):
    """expose_recommended_entities raises for a device_id that isn't registered."""
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPOSE_RECOMMENDED_ENTITIES,
            {"device_id": "does-not-exist"},
            blocking=True,
        )


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


async def test_persistent_loss_raises_error_repair_and_keeps_cache(
    hass, mock_client, mock_config_entry
):
    """A persistent device loss raises a loud fixable ERROR repair and does NOT
    persist the reduced topology or reload — the full prior stays cached."""
    prior = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32, 0x33],
        bcu_stacks=[],
    )
    actual = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mismatch = PlantTopologyMismatch("battery missing", prior=prior, actual=actual)
    mock_client.detect = AsyncMock(side_effect=mismatch)

    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.givenergy_local._load_capabilities",
            new=AsyncMock(return_value=prior),
        ),
        patch("custom_components.givenergy_local._save_capabilities", new=AsyncMock()) as save_mock,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ) as reload_mock,
        patch("custom_components.givenergy_local.coordinator.asyncio.sleep", AsyncMock()),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Reduced topology NOT persisted, integration NOT reloaded.
    save_mock.assert_not_awaited()
    reload_mock.assert_not_called()
    # A loud, fixable ERROR repair names the missing battery.
    issue = ir.async_get(hass).issues.get(
        (DOMAIN, f"expected_devices_missing_{mock_config_entry.entry_id}")
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is True
    assert "battery at 0x33" in issue.translation_placeholders["devices"]


async def test_full_topology_detect_clears_stale_missing_repair(
    hass, mock_client, mock_config_entry
):
    """A successful detect (full topology) clears any standing device-missing repair."""
    mock_config_entry.add_to_hass(hass)
    # Pre-seed a stale repair as if a prior run had flagged a missing device.
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"expected_devices_missing_{mock_config_entry.entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="expected_devices_missing",
        translation_placeholders={"devices": "battery at 0x33"},
        data={"entry_id": mock_config_entry.entry_id},
    )

    # Default mock_client.detect succeeds (no mismatch) → healed callback fires.
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        DOMAIN,
        f"expected_devices_missing_{mock_config_entry.entry_id}",
    ) not in ir.async_get(hass).issues


async def test_heal_reloads_when_recovered_device_missed_setup(
    hass, mock_client, mock_config_entry
):
    """A device absent when entities were created gets them via an entry reload once
    the topology heals (#148): the healed callback diffs the confirmed topology
    against the setup-time snapshot and schedules a reload on any gap."""
    # Default fixture topology: one battery at 0x32.
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    # Mid-session heal confirms a battery (0x33) that wasn't there at setup.
    confirmed = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32, 0x33],
        bcu_stacks=[],
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_mock:
        await coordinator._on_topology_healed(confirmed)
    reload_mock.assert_called_once_with(mock_config_entry.entry_id)


async def test_heal_matching_setup_topology_does_not_reload(hass, mock_client, mock_config_entry):
    """The routine heal — confirmed topology identical to what setup instantiated —
    must not reload (it fires on every clean reconnect)."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    confirmed = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_mock:
        await coordinator._on_topology_healed(confirmed)
    reload_mock.assert_not_called()


async def test_unload_entry_shuts_down_coordinator_before_closing(
    hass, mock_client, mock_config_entry
):
    """Unload must shut the coordinator down (no new scheduled refresh can race
    teardown) before discarding the client. HA's auto-registered shutdown (via
    config_entry.async_on_unload) fires only after async_unload_entry returns —
    too late, the client is already gone by then."""
    from unittest.mock import Mock

    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    order = Mock()
    with (
        patch.object(
            coordinator, "async_shutdown", wraps=coordinator.async_shutdown
        ) as shutdown_spy,
        patch.object(coordinator, "async_close", wraps=coordinator.async_close) as close_spy,
    ):
        order.attach_mock(shutdown_spy, "shutdown")
        order.attach_mock(close_spy, "close")
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    shutdown_spy.assert_awaited()
    close_spy.assert_awaited_once()
    spy_calls = [c[0] for c in order.mock_calls if c[0] in ("shutdown", "close")]
    assert spy_calls.index("shutdown") < spy_calls.index("close")


async def test_missing_device_fix_flow_clears_cache_and_reloads(hass):
    """The repair's Fix step clears the entry's cached topology and reloads it."""
    from custom_components.givenergy_local.repairs import (
        ExpectedDevicesMissingRepairFlow,
    )

    flow = ExpectedDevicesMissingRepairFlow({"entry_id": "entry-xyz", "devices": "battery at 0x33"})
    flow.hass = hass

    fake_store = AsyncMock()
    with (
        patch(
            "custom_components.givenergy_local._capabilities_store",
            return_value=fake_store,
        ) as store_factory,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_mock,
    ):
        result = await flow.async_step_init({})

    store_factory.assert_called_once_with(hass, "entry-xyz")
    fake_store.async_remove.assert_awaited_once()
    reload_mock.assert_called_once_with("entry-xyz")
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_missing_device_fix_flow_form_names_devices(hass):
    """Before confirming, the Fix form surfaces which device(s) went missing."""
    from custom_components.givenergy_local.repairs import (
        ExpectedDevicesMissingRepairFlow,
    )

    flow = ExpectedDevicesMissingRepairFlow({"entry_id": "e", "devices": "battery at 0x33"})
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == FlowResultType.FORM
    assert result["description_placeholders"]["devices"] == "battery at 0x33"


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

    mock_client.detect.assert_awaited_once()
    assert mock_client.detect.await_args.kwargs["prior"] is None
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


async def test_warm_start_freshens_persisted_capabilities_on_drift(
    hass, mock_client, mock_config_entry
):
    """A warm start whose live capabilities differ from the loaded cache must
    re-persist the live ones and update the coordinator's reconnect hint.
    Motivating case: givenergy-modbus 2.3.0 retired the 0x31 read-alias (#249) —
    a cache persisted with inverter_address=0x31 keeps working only via the
    hardware facade and is expected to self-heal via the consumer re-persisting
    after detect()."""
    stale_prior = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x31,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.givenergy_local._load_capabilities",
            new=AsyncMock(return_value=stale_prior),
        ),
        patch("custom_components.givenergy_local._save_capabilities", new=AsyncMock()) as save_mock,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Live capabilities (conftest: inverter_address=0x32) drifted from the
    # 0x31 cache → re-persisted, and the reconnect hint follows the wire.
    save_mock.assert_awaited_once_with(
        hass, mock_config_entry.entry_id, mock_client.plant.capabilities
    )
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator._prior_capabilities is mock_client.plant.capabilities


async def test_warm_start_with_matching_cache_does_not_rewrite(
    hass, mock_client, mock_config_entry
):
    """A warm hit whose cache structurally equals the live capabilities must not
    write — the historic write-avoidance stays for the steady state."""
    matching_prior = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.givenergy_local._load_capabilities",
            new=AsyncMock(return_value=matching_prior),
        ),
        patch("custom_components.givenergy_local._save_capabilities", new=AsyncMock()) as save_mock,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

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
    for key in ("p_pv", "battery_soc", "grid_power", "p_load_demand", "status"):
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


async def test_upgrade_removes_unreadable_control_rows(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """#207: on upgrade, orphaned rows for readability-gated controls whose register
    reads None are removed pre-platform — suppressing creation alone leaves them."""
    mock_inverter.battery_charge_limit_ac = None
    mock_inverter.battery_discharge_limit_ac = None
    mock_inverter.battery_pause_mode = None  # pause absent → mode + slots gated
    mock_inverter.system_time = None  # clock register absent → datetime gated (#219)
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Rows a prior version created: readability-gated controls + one readable control.
    seeded = (
        ("number", "SA1234G123_battery_charge_limit_ac"),
        ("number", "SA1234G123_battery_discharge_limit_ac"),
        ("select", "SA1234G123_battery_pause_mode"),
        ("time", "SA1234G123_battery_pause_slot_start"),
        ("time", "SA1234G123_battery_pause_slot_end"),
        ("datetime", "SA1234G123_system_time"),
        ("select", "SA1234G123_battery_power_mode"),  # readable → must survive
    )
    for domain, unique_id in seeded:
        registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=mock_config_entry)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    def _present(domain: str, key: str) -> bool:
        return registry.async_get_entity_id(domain, DOMAIN, f"SA1234G123_{key}") is not None

    # Readability-gated controls whose register reads None are removed.
    assert not _present("number", "battery_charge_limit_ac")
    assert not _present("number", "battery_discharge_limit_ac")
    assert not _present("select", "battery_pause_mode")
    assert not _present("time", "battery_pause_slot_start")
    assert not _present("time", "battery_pause_slot_end")
    assert not _present("datetime", "system_time")
    # A readable control is untouched.
    assert _present("select", "battery_power_mode")


async def test_reconcile_keeps_controls_on_partial_seed(hass, mock_client, setup_integration):
    """#208: a partial seed poll (last_partial_failures set) must NOT remove control
    rows — a None read could be a transient bank failure, recoverable on a later poll."""
    from custom_components.givenergy_local import _reconcile_readability_gated_controls

    coordinator = hass.data[DOMAIN][setup_integration.entry_id]
    coordinator.last_partial_failures = [object()]  # partial seed
    coordinator.data.inverter.battery_charge_limit_ac = None  # reads None (transient)
    coordinator.data.inverter.system_time = None  # reads None (transient)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "number", DOMAIN, "SA1234G123_battery_charge_limit_ac", config_entry=setup_integration
    )
    registry.async_get_or_create(
        "datetime", DOMAIN, "SA1234G123_system_time", config_entry=setup_integration
    )

    _reconcile_readability_gated_controls(hass, coordinator)

    # Partial seed → reconciliation is a no-op; the rows are retained.
    assert (
        registry.async_get_entity_id("number", DOMAIN, "SA1234G123_battery_charge_limit_ac")
        is not None
    )
    assert registry.async_get_entity_id("datetime", DOMAIN, "SA1234G123_system_time") is not None


async def test_upgrade_removes_dc_limit_rows_on_ac_coupled(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """#52: on an AC-coupled / AIO plant the DC battery-limit controls are suppressed
    in favour of the AC pair. On upgrade, the orphaned DC rows a prior version created
    are removed pre-platform — suppressing creation alone would leave them behind."""
    mock_plant.capabilities = PlantCapabilities(
        device_type=Model.AC,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    mock_inverter.battery_charge_limit_ac = 50
    mock_inverter.battery_discharge_limit_ac = 60
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique_id in ("SA1234G123_battery_charge_limit", "SA1234G123_battery_discharge_limit"):
        registry.async_get_or_create("number", DOMAIN, unique_id, config_entry=mock_config_entry)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    def _present(key: str) -> bool:
        return registry.async_get_entity_id("number", DOMAIN, f"SA1234G123_{key}") is not None

    # The DC pair is the wrong register here, so its stale rows are removed...
    assert not _present("battery_charge_limit")
    assert not _present("battery_discharge_limit")
    # ...while the AC pair remains the battery-power control.
    assert _present("battery_charge_limit_ac")
    assert _present("battery_discharge_limit_ac")


async def test_dc_limit_rows_retained_on_hybrid(
    hass, mock_client, mock_plant, mock_inverter, mock_config_entry
):
    """Regression guard: a DC-coupled hybrid keeps its DC battery-limit rows on
    upgrade — the AC-coupled suppression must not touch them."""
    # mock_plant defaults to device_type=Model.HYBRID (no AC-config block).
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique_id in ("SA1234G123_battery_charge_limit", "SA1234G123_battery_discharge_limit"):
        registry.async_get_or_create("number", DOMAIN, unique_id, config_entry=mock_config_entry)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    def _present(key: str) -> bool:
        return registry.async_get_entity_id("number", DOMAIN, f"SA1234G123_{key}") is not None

    assert _present("battery_charge_limit")
    assert _present("battery_discharge_limit")
