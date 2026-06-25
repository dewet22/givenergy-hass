"""Tests for the GivEnergy Local config flow."""

from unittest.mock import patch

from givenergy_modbus.exceptions import RefreshFailed, RefreshPartiallySucceeded
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.givenergy_local.const import (
    CONF_BATTERY_DATA_ONLY,
    CONF_EXPERIMENTAL,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    ExperimentalFeature,
)

VALID_USER_INPUT = {
    CONF_HOST: "192.168.1.100",
    CONF_PORT: 8899,
    CONF_SCAN_INTERVAL: 30,
    CONF_PASSIVE: False,
}


async def test_form_renders(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_successful_setup_creates_entry(hass, mock_client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "GivEnergy SA1234G123"
    assert result["data"] == VALID_USER_INPUT


async def test_setup_with_battery_data_only_sets_option(hass, mock_client):
    """Ticking battery-data-only at add time lands in entry.options (where it's read
    everywhere), not data — so a parallel-mode AIO can be added without its
    inverter-level sensors ever being created then going unavailable (#95)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID_USER_INPUT, CONF_BATTERY_DATA_ONLY: True}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["options"] == {CONF_BATTERY_DATA_ONLY: True}
    assert CONF_BATTERY_DATA_ONLY not in result["data"]
    assert result["data"] == VALID_USER_INPUT


async def test_cannot_connect_shows_error(hass, mock_client):
    mock_client.connect.side_effect = ConnectionRefusedError()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_partial_success_during_setup_returns_serial(hass, mock_client, mock_plant):
    """A partial poll during the connection test still identifies the inverter:
    the serial (device 0x32) is virtually always among the reads that succeeded."""
    mock_client.refresh.side_effect = RefreshPartiallySucceeded(
        "partial",
        plant=mock_plant,
        failures=[],
        cause=ExceptionGroup("reads", [TimeoutError()]),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "GivEnergy SA1234G123"


async def test_partial_without_serial_during_setup_shows_cannot_connect(
    hass, mock_client, mock_plant
):
    """If the partial dropped the inverter read itself (no serial), there's no
    usable unique ID — treat it as cannot_connect rather than a blank entry."""
    mock_plant.inverter_serial_number = ""
    mock_client.refresh.side_effect = RefreshPartiallySucceeded(
        "partial",
        plant=mock_plant,
        failures=[],
        cause=ExceptionGroup("reads", [TimeoutError()]),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_refresh_failed_during_setup_shows_cannot_connect(hass, mock_client):
    """A total failure (no data at all) during the connection test → cannot_connect."""
    mock_client.refresh.side_effect = RefreshFailed(
        "link dead",
        failures=[],
        cause=ExceptionGroup("reads", [TimeoutError()]),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_entry_aborted(hass, mock_client, mock_config_entry):
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID_USER_INPUT)

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reconfigure_form_is_prefilled(hass, mock_client, setup_integration):
    result = await setup_integration.start_reconfigure_flow(hass)

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    # Suggested values are pre-filled from the entry's current data
    suggested = {
        key.schema: key.description.get("suggested_value")
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested[CONF_HOST] == "192.168.1.100"
    assert suggested[CONF_SCAN_INTERVAL] == 30
    assert suggested[CONF_PASSIVE] is False


async def test_reconfigure_updates_settings_without_retesting_connection(
    hass, mock_client, setup_integration
):
    """Changing only scan_interval/passive should skip the explicit connection test."""
    mock_client.refresh.reset_mock()  # ignore the initial setup refresh
    mock_client.load_config.reset_mock()

    result = await setup_integration.start_reconfigure_flow(hass)
    new_input = {**VALID_USER_INPUT, CONF_SCAN_INTERVAL: 60, CONF_PASSIVE: True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_SCAN_INTERVAL] == 60
    assert setup_integration.data[CONF_PASSIVE] is True

    # _test_connection (host/port change only) issues a bare refresh() with no
    # preceding load_config(); the post-reload coordinator always pairs the two
    # on its first (full) tick. Equal counts ⇒ _test_connection did not run.
    assert mock_client.refresh.call_count == mock_client.load_config.call_count


async def test_reconfigure_with_host_change_succeeds_for_same_inverter(
    hass, mock_client, setup_integration
):
    """Changing host re-tests the connection; same serial means it's still our inverter."""
    result = await setup_integration.start_reconfigure_flow(hass)
    new_input = {**VALID_USER_INPUT, CONF_HOST: "192.168.1.200"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_HOST] == "192.168.1.200"


async def test_reconfigure_rejects_different_inverter(
    hass, mock_client, mock_plant, setup_integration
):
    """If the new host serves a different serial, refuse — would orphan entities."""
    mock_plant.inverter_serial_number = "DIFFERENT_SERIAL"

    result = await setup_integration.start_reconfigure_flow(hass)
    new_input = {**VALID_USER_INPUT, CONF_HOST: "192.168.1.200"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "wrong_inverter"}
    # Entry is unchanged
    assert setup_integration.data[CONF_HOST] == "192.168.1.100"


async def test_reconfigure_cannot_connect_shows_error(hass, mock_client, setup_integration):
    """If the new host is unreachable, show cannot_connect and keep the entry intact."""
    mock_client.connect.side_effect = ConnectionRefusedError()

    result = await setup_integration.start_reconfigure_flow(hass)
    new_input = {**VALID_USER_INPUT, CONF_HOST: "192.168.1.200"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}
    assert setup_integration.data[CONF_HOST] == "192.168.1.100"


async def test_options_flow_sets_battery_data_only(hass, mock_client, setup_integration):
    """The options flow persists the battery-data-only toggle and reloads the entry."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BATTERY_DATA_ONLY: True}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_BATTERY_DATA_ONLY] is True
    # The update listener reloaded the entry, so it's loaded and serving again.
    assert setup_integration.state is config_entries.ConfigEntryState.LOADED


async def test_options_flow_prefills_existing_value(hass, mock_client, setup_integration):
    """Re-opening the options form when the value is already True must pre-fill True,
    proving add_suggested_values_to_schema round-trips the saved option."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BATTERY_DATA_ONLY: True}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] == "form"
    markers = {marker.schema: marker for marker in result["data_schema"].schema}
    suggested = markers[CONF_BATTERY_DATA_ONLY].description.get("suggested_value")
    assert suggested is True


# --- Experimental features section (client feature-flagging) ------------------


_DEMO_FEATURE = ExperimentalFeature(conf_key="demo", client_kwarg="demo_kwarg")


async def test_options_flow_renders_experimental_section_when_features_exist(
    hass, mock_client, setup_integration
):
    """With a feature registered, the options form carries a collapsed
    'experimental' section alongside the battery-data-only toggle."""
    with patch(
        "custom_components.givenergy_local.config_flow.EXPERIMENTAL_FEATURES",
        (_DEMO_FEATURE,),
    ):
        result = await hass.config_entries.options.async_init(setup_integration.entry_id)

    assert result["type"] == "form"
    top_keys = {marker.schema for marker in result["data_schema"].schema}
    assert CONF_EXPERIMENTAL in top_keys
    assert CONF_BATTERY_DATA_ONLY in top_keys


async def test_options_flow_persists_experimental_toggle(hass, mock_client, setup_integration):
    """Submitting the nested section dict lands under entry.options[experimental]."""
    with patch(
        "custom_components.givenergy_local.config_flow.EXPERIMENTAL_FEATURES",
        (_DEMO_FEATURE,),
    ):
        result = await hass.config_entries.options.async_init(setup_integration.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_BATTERY_DATA_ONLY: False, CONF_EXPERIMENTAL: {"demo": True}},
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_EXPERIMENTAL] == {"demo": True}


async def test_options_flow_omits_section_when_no_features(hass, mock_client, setup_integration):
    """The shipped empty registry => no experimental section, battery_data_only intact."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    top_keys = {marker.schema for marker in result["data_schema"].schema}
    assert CONF_EXPERIMENTAL not in top_keys
    assert CONF_BATTERY_DATA_ONLY in top_keys
