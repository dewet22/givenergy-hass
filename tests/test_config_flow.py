"""Tests for the GivEnergy Local config flow."""

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.givenergy_local.const import (
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
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


async def test_cannot_connect_shows_error(hass, mock_client):
    mock_client.connect.side_effect = ConnectionRefusedError()

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
    mock_client.refresh_plant.reset_mock()  # ignore the initial setup refresh

    result = await setup_integration.start_reconfigure_flow(hass)
    new_input = {**VALID_USER_INPUT, CONF_SCAN_INTERVAL: 60, CONF_PASSIVE: True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_SCAN_INTERVAL] == 60
    assert setup_integration.data[CONF_PASSIVE] is True

    # The post-reload coordinator calls refresh_plant(full_refresh=True).
    # _test_connection (used only when host/port changes) calls
    # refresh_plant(full_refresh=False) — the latter should not appear if
    # the host didn't change.
    test_connection_calls = [
        c for c in mock_client.refresh_plant.call_args_list if c.kwargs.get("full_refresh") is False
    ]
    assert test_connection_calls == []


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
