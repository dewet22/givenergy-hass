"""Adds config flow for GivEnergy."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from givenergy_modbus.client.client import Client
from givenergy_modbus.exceptions import CommunicationError
from givenergy_modbus.model.plant import Plant
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DOMAIN, LOGGER, CONF_REFRESH_INTERVAL, CONF_FULL_REFRESH_INTERVAL

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)),
        vol.Optional(CONF_PORT, default=8899): int,
        vol.Optional(CONF_REFRESH_INTERVAL, default=30): int,
        vol.Optional(CONF_FULL_REFRESH_INTERVAL, default=60): int,
    }
)


class ConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow handler for GivEnergy."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.FlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            try:
                p = await self._test_connection(host=user_input[CONF_HOST], port=user_input[CONF_PORT])
            except CommunicationError as e:
                LOGGER.error(e)
                _errors["base"] = "connection"
            except Exception as e:
                LOGGER.exception(e)
                _errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f'Plant {p.data_adapter_serial_number}',
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, user_input),
            errors=_errors,
        )

    async def _test_connection(self, host: str, port: int) -> Plant:
        """Validate remote system is alive and responding."""
        client = Client(host=host, port=port)
        await client.connect()
        p = await client.refresh_plant(True)
        await client.close()
        return p

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow handler for GivEnergy."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)
