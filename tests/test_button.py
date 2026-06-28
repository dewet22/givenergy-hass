"""Tests for the button platform: Restart (inverter reboot) and Re-detect Plant."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.givenergy_local.const import DOMAIN


def _entity_id(hass, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("button", DOMAIN, unique_id)


async def _press(hass, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


# --- Restart button (inverter reboot) ---------------------------------------


async def test_restart_button_created_on_inverter(hass, setup_integration):
    """A directly-connected inverter gets a Restart button with the RESTART class."""
    entity_id = _entity_id(hass, "SA1234G123_restart")
    assert entity_id is not None
    assert hass.states.get(entity_id).attributes["device_class"] == "restart"


async def test_restart_button_press_sends_reboot(hass, mock_client, setup_integration):
    """Pressing Restart issues the one-shot inverter reboot command."""
    await _press(hass, _entity_id(hass, "SA1234G123_restart"))
    mock_client.one_shot_command.assert_called_once()


async def test_restart_button_raises_when_disconnected(hass, mock_client, setup_integration):
    """A press while the client is disconnected surfaces a clean error and sends nothing."""
    mock_client.connected = False
    with pytest.raises(HomeAssistantError):
        await _press(hass, _entity_id(hass, "SA1234G123_restart"))
    mock_client.one_shot_command.assert_not_called()


async def test_restart_button_absent_on_ems(hass, ems_setup):
    """The reboot register (HR163) isn't in the EMS controller's write-safe set, so no
    Restart button is created on an EMS plant (mirrors the other control platforms)."""
    assert _entity_id(hass, "SA1234G123_restart") is None


# --- Re-detect Plant button --------------------------------------------------


async def test_redetect_button_created_on_inverter(hass, setup_integration):
    """A directly-connected inverter gets the Re-detect Plant button."""
    assert _entity_id(hass, "SA1234G123_redetect_plant") is not None


async def test_redetect_button_present_on_ems(hass, ems_setup):
    """Re-detect is a safe read-side reload, so it's offered even on the EMS controller
    (which otherwise exposes no controls)."""
    assert _entity_id(hass, "SA1234G123_redetect_plant") is not None


async def test_redetect_button_press_reloads_entry(hass, setup_integration):
    """Pressing Re-detect clears the cached topology and schedules a reload — the
    in-UI equivalent of the redetect_plant service."""
    entity_id = _entity_id(hass, "SA1234G123_redetect_plant")
    fake_store = AsyncMock()
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        patch("custom_components.givenergy_local._capabilities_store", return_value=fake_store),
    ):
        await _press(hass, entity_id)
    fake_store.async_remove.assert_awaited_once()
    reload.assert_called_once_with(setup_integration.entry_id)
