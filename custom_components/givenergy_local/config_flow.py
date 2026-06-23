from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from givenergy_modbus.client.client import Client
from givenergy_modbus.exceptions import RefreshPartiallySucceeded
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_BATTERY_DATA_ONLY,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    DEFAULT_BATTERY_DATA_ONLY,
    DEFAULT_PASSIVE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
        vol.Required(CONF_PASSIVE, default=DEFAULT_PASSIVE): bool,
    }
)


class GivEnergyLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GivEnergyLocalOptionsFlow:
        return GivEnergyLocalOptionsFlow()

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
            # detect() resolves the device model and topology before any reads
            # so refresh() picks the right register layout (single vs.
            # three-phase) from the first request.
            await client.detect()
            try:
                plant = await client.refresh()
            except RefreshPartiallySucceeded as exc:
                # A connectivity probe only needs to identify the inverter
                # (device 0x32), which is virtually always among the successful
                # reads — a partial usually means a peripheral battery/meter
                # dropped. A usable snapshot is enough here; RefreshFailed (no
                # data at all) falls through to "cannot_connect" below.
                plant = exc.plant
            serial = plant.inverter_serial_number
            if not serial:
                # The partial dropped the inverter read itself — no usable
                # unique ID, so treat it as a failed connection rather than
                # creating an entry with an empty serial.
                return "", "cannot_connect"
            return serial, None
        except Exception:
            _LOGGER.exception("Connection test failed for %s:%s", host, port)
            return "", "cannot_connect"
        finally:
            await client.close()


class GivEnergyLocalOptionsFlow(OptionsFlow):
    """Per-entry options.

    Currently just the battery-data-only toggle (#95): for a unit controlled by a
    Gateway in a parallel group, suppress its control entities and inverter-level
    system sensors, leaving only battery / HV-stack / module / diagnostic data.
    Changing it reloads the entry (see the update listener in __init__).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_BATTERY_DATA_ONLY, default=DEFAULT_BATTERY_DATA_ONLY): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )
