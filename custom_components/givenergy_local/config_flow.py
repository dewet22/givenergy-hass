from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from givenergy_modbus.client.client import Client
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import (
    CONF_MAX_BATTERIES,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT_TOLERANCE,
    DEFAULT_MAX_BATTERIES,
    DEFAULT_PASSIVE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT_TOLERANCE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
        vol.Required(CONF_MAX_BATTERIES, default=DEFAULT_MAX_BATTERIES): int,
        vol.Required(CONF_PASSIVE, default=DEFAULT_PASSIVE): bool,
        vol.Required(CONF_TIMEOUT_TOLERANCE, default=DEFAULT_TIMEOUT_TOLERANCE): int,
    }
)


class GivEnergyLocalConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            serial, err = await self._test_connection(host, port)
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"GivEnergy {serial}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update an existing entry's settings (scan interval, passive mode, …)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            connection_changed = host != entry.data[CONF_HOST] or port != entry.data[CONF_PORT]

            if connection_changed:
                serial, err = await self._test_connection(host, port)
                if err:
                    errors["base"] = err
                elif serial != entry.unique_id:
                    # Connecting to a different inverter would corrupt the
                    # entity registry; require a fresh integration instead.
                    errors["base"] = "wrong_inverter"

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_DATA_SCHEMA, entry.data),
            errors=errors,
        )

    async def _test_connection(self, host: str, port: int) -> tuple[str, str | None]:
        client = Client(host=host, port=port)
        try:
            await client.connect()
            plant = await client.refresh_plant(full_refresh=False, max_batteries=0)
            return plant.inverter_serial_number, None
        except Exception:
            _LOGGER.exception("Connection test failed for %s:%s", host, port)
            return "", "cannot_connect"
        finally:
            await client.close()
