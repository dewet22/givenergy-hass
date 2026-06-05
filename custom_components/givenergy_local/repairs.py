"""Repairs flow for the GivEnergy Local integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, SERVICE_GENERATE_DASHBOARD


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    return DashboardOutdatedRepairFlow(data)


class DashboardOutdatedRepairFlow(RepairsFlow):
    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_GENERATE_DASHBOARD,
                {"max_power_kw": self._data.get("max_power_kw", 10)},
                blocking=True,
            )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={
                "old_version": str(self._data.get("old_version", "?")),
                "new_version": str(self._data.get("new_version", "?")),
            },
        )
