"""Shared fixtures for GivEnergy Local tests."""
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.inverter import BatteryPowerMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_inverter() -> MagicMock:
    inv = MagicMock()
    inv.status = MagicMock()
    inv.status.name = "NORMAL"
    inv.fault_code = "00000000"
    inv.model = MagicMock()
    inv.model.name = "HYBRID"
    inv.firmware_version = "D0.19-A0.21"
    inv.work_time_total = 36_000_000  # 10,000 hours in seconds
    inv.p_pv.return_value = 2500
    inv.p_pv1 = 1500
    inv.p_pv2 = 1000
    inv.v_pv1 = 380.5
    inv.v_pv2 = 375.2
    inv.i_pv1 = 3.94
    inv.i_pv2 = 2.67
    inv.e_pv_day.return_value = 12.4
    inv.e_pv1_day = 7.2
    inv.e_pv2_day = 5.2
    inv.e_pv_total = 4521.8
    inv.battery_soc = 85
    inv.p_battery = 500
    inv.v_battery = 52.4
    inv.i_battery = 9.54
    inv.t_battery = 22.5
    inv.e_battery_charge_day = 8.2
    inv.e_battery_discharge_day = 3.1
    inv.e_battery_throughput = 1250.3
    inv.p_grid_out = -800
    inv.e_grid_out_day = 2.1
    inv.e_grid_in_day = 5.3
    inv.e_grid_out_total = 892.4
    inv.e_grid_in_total = 1234.5
    inv.v_ac1 = 240.1
    inv.f_ac1 = 50.02
    inv.p_load_demand = 1200
    inv.e_load_day = 9.8
    inv.e_inverter_out_day = 11.2
    inv.e_inverter_out_total = 5100.2
    inv.t_inverter_heatsink = 45.3
    inv.t_charger = 38.7
    inv.enable_charge = True
    inv.enable_discharge = True
    inv.charge_target_soc = 100
    inv.battery_soc_reserve = 4
    inv.battery_charge_limit = 50
    inv.battery_discharge_limit = 50
    inv.battery_discharge_min_power_reserve = 4
    inv.battery_power_mode = BatteryPowerMode.SELF_CONSUMPTION
    inv.system_time = datetime(2026, 5, 10, 12, 0, 0)
    inv.charge_slot_1 = TimeSlot(start=time(0, 30), end=time(4, 30))
    inv.charge_slot_2 = TimeSlot(start=time(0, 0), end=time(0, 0))
    inv.discharge_slot_1 = TimeSlot(start=time(17, 0), end=time(22, 0))
    inv.discharge_slot_2 = TimeSlot(start=time(0, 0), end=time(0, 0))
    return inv


@pytest.fixture
def mock_battery() -> MagicMock:
    bat = MagicMock()
    bat.serial_number = "BT1234A001"
    bat.soc = 85
    bat.v_out = 52.4
    bat.t_max = 24.5
    bat.t_min = 21.3
    bat.cap_remaining = 8.5
    bat.cap_design = 9.5
    bat.num_cycles = 42
    bat.bms_firmware_version = 3005
    return bat


@pytest.fixture
def mock_plant(mock_inverter, mock_battery) -> MagicMock:
    plant = MagicMock()
    plant.inverter_serial_number = "SA1234G123"
    plant.data_adapter_serial_number = "WF1234G456"
    plant.inverter = mock_inverter
    plant.batteries = [mock_battery]
    plant.number_batteries = 1
    return plant


@pytest.fixture
def mock_client(mock_plant) -> AsyncMock:
    client = AsyncMock()
    client.connected = True
    client.plant = mock_plant
    client.refresh_plant = AsyncMock(return_value=mock_plant)
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.one_shot_command = AsyncMock()
    with (
        patch("custom_components.givenergy_local.coordinator.Client", return_value=client),
        patch("custom_components.givenergy_local.config_flow.Client", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 8899,
            "scan_interval": 30,
            "max_batteries": 1,
            "passive": False,
        },
        unique_id="SA1234G123",
    )


@pytest.fixture
async def setup_integration(hass, mock_client, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
