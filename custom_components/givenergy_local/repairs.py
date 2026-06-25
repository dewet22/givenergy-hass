"""Repairs flow for the GivEnergy Local integration."""

from __future__ import annotations

import voluptuous as vol
from givenergy_modbus.client import commands
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    if issue_id.startswith("expected_devices_missing_"):
        return ExpectedDevicesMissingRepairFlow(data)
    if issue_id.startswith("system_time_drift_"):
        return SystemTimeDriftRepairFlow(data)
    raise ValueError(f"Unknown fixable issue: {issue_id}")


class ExpectedDevicesMissingRepairFlow(RepairsFlow):
    """Fix flow for a previously-known device that stopped responding.

    Confirming clears the cached plant topology for the entry and reloads it —
    a fresh cold detect() then commits whatever hardware is actually present
    (the same effect as the redetect_plant service, but targeted directly by
    the entry_id we already hold).
    """

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            # Lazy import to avoid any import cycle with the package __init__.
            from . import _capabilities_store

            entry_id = self._data.get("entry_id")
            if entry_id:
                await _capabilities_store(self.hass, entry_id).async_remove()
                self.hass.config_entries.async_schedule_reload(entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={
                "devices": str(self._data.get("devices", "a device")),
            },
        )


class SystemTimeDriftRepairFlow(RepairsFlow):
    """Fix flow for a drifted inverter clock.

    Confirming writes the current Home Assistant time to the inverter's RTC — the
    same operation as the set_system_datetime service — then requests a refresh, so
    the drift re-evaluates and the standing repair clears on the next read.
    """

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            entry_id = self._data.get("entry_id")
            coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id) if entry_id else None
            client = getattr(coordinator, "_client", None)
            if coordinator is None or client is None or not client.connected:
                raise HomeAssistantError("GivEnergy inverter is not currently connected")
            await client.one_shot_command(commands.set_system_date_time(dt_util.now()))
            await coordinator.async_request_refresh()
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={
                "drift_minutes": str(self._data.get("drift_minutes", "")),
                "system_time": str(self._data.get("system_time", "")),
                "ha_time": str(self._data.get("ha_time", "")),
            },
        )
