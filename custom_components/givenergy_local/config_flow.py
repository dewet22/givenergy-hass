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
from homeassistant.data_entry_flow import SectionConfig, section

from .const import (
    CONF_BATTERY_DATA_ONLY,
    CONF_EXPERIMENTAL,
    CONF_PASSIVE,
    CONF_SCAN_INTERVAL,
    CONF_WARN_CLOCK_DRIFT,
    DEFAULT_BATTERY_DATA_ONLY,
    DEFAULT_PASSIVE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WARN_CLOCK_DRIFT,
    DOMAIN,
    EXPERIMENTAL_FEATURES,
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

# Initial setup also offers battery-data-only, so a parallel-mode AIO can be added
# without its inverter-level sensors ever being created and then going unavailable
# (#95). Reconfigure uses the base schema — the toggle lives in options thereafter.
STEP_USER_SETUP_SCHEMA = STEP_USER_DATA_SCHEMA.extend(
    {vol.Required(CONF_BATTERY_DATA_ONLY, default=DEFAULT_BATTERY_DATA_ONLY): bool}
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
                # The toggle is read from entry.options everywhere (it's normally
                # set via the options flow), so split it out of data into options
                # rather than leaving it in data where nothing would read it.
                battery_data_only = user_input.pop(
                    CONF_BATTERY_DATA_ONLY, DEFAULT_BATTERY_DATA_ONLY
                )
                return self.async_create_entry(
                    title=f"GivEnergy {serial}",
                    data=user_input,
                    options={CONF_BATTERY_DATA_ONLY: battery_data_only},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SETUP_SCHEMA,
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
            # user_input carries battery_data_only flat, plus — when any experimental
            # feature is registered — the section's toggles nested under
            # CONF_EXPERIMENTAL. Persisted as-is; resolve_experimental_client_kwargs
            # reads that nested shape at coordinator construction.
            return self.async_create_entry(data=user_input)
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_BATTERY_DATA_ONLY, default=DEFAULT_BATTERY_DATA_ONLY): bool,
            vol.Required(CONF_WARN_CLOCK_DRIFT, default=DEFAULT_WARN_CLOCK_DRIFT): bool,
        }
        # Surface the collapsed "Experimental features" group only once at least one
        # flag exists, so the header never appears empty (the registry ships empty).
        if EXPERIMENTAL_FEATURES:
            # Seed schema defaults from the currently-saved values so that a
            # collapsed or omitted section round-trips correctly.  This works
            # because HA's frontend either (a) submits the rendered (pre-filled)
            # values for a collapsed section, in which case the submitted value
            # wins, or (b) omits the section key entirely, in which case the
            # vol.Optional / inner vol.Required defaults fill in the existing
            # values.  The two cases are indistinguishable if the frontend sends
            # base defaults (all False) for untouched collapsed sections — but
            # HA keeps section inputs in the DOM, so (a) is what happens.
            existing_exp: dict[str, Any] = self.config_entry.options.get(CONF_EXPERIMENTAL, {})
            schema_dict[vol.Optional(CONF_EXPERIMENTAL, default=existing_exp)] = section(
                vol.Schema(
                    {
                        vol.Required(
                            feature.conf_key,
                            default=existing_exp.get(feature.conf_key, feature.default),
                        ): bool
                        for feature in EXPERIMENTAL_FEATURES
                    }
                ),
                SectionConfig(collapsed=True),
            )
        schema = vol.Schema(schema_dict)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )
