"""Repairs flow for the GivEnergy Local integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    if issue_id.startswith("expected_devices_missing_"):
        return ExpectedDevicesMissingRepairFlow(data)
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
