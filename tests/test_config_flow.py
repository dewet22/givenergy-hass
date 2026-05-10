"""Tests for the GivEnergy Local config flow."""
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.givenergy_local.const import (
    CONF_MAX_BATTERIES,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

VALID_USER_INPUT = {
    CONF_HOST: "192.168.1.100",
    CONF_PORT: 8899,
    CONF_SCAN_INTERVAL: 30,
    CONF_MAX_BATTERIES: 1,
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
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "GivEnergy SA1234G123"
    assert result["data"] == VALID_USER_INPUT


async def test_cannot_connect_shows_error(hass, mock_client):
    mock_client.connect.side_effect = ConnectionRefusedError()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_USER_INPUT
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_entry_aborted(hass, mock_client, mock_config_entry):
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_USER_INPUT
    )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
