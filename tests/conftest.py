"""Shared fixtures for GivEnergy Local tests."""

from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.battery import Battery
from givenergy_modbus.model.inverter import (
    SINGLE_PHASE_SLOTS,
    BatteryPowerMode,
    BatteryType,
    ChargeStatus,
    MeterType,
    Model,
    SinglePhaseInverter,
)
from givenergy_modbus.model.plant import PlantCapabilities
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.givenergy_local.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_inverter() -> MagicMock:
    inv = MagicMock()
    # Delegate precision_of to the real model classmethod so the display-precision
    # derivation is exercised against the library's actual register scaling.
    inv.precision_of = SinglePhaseInverter.precision_of
    inv.status = MagicMock()
    inv.status.name = "NORMAL"
    inv.fault_code = "00000000"
    inv.model = MagicMock()
    inv.model.name = "HYBRID"
    inv.firmware_version = "D0.19-A0.21"
    inv.work_time_total_hours = 36055  # hours of operation (raw register unit)
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
    # Canonical battery-energy field names (givenergy-modbus #76). Mirror the
    # real model so the mock can't fabricate fields the sensors then break on.
    inv.e_battery_charge_today = 8.2
    inv.e_battery_discharge_today = 3.1
    inv.e_battery_charge_total = 980.5
    inv.e_battery_discharge_total = 845.1
    inv.e_battery_throughput = 1250.3
    inv.p_grid_out = -800
    inv.e_grid_out_day = 2.1
    inv.e_grid_in_day = 5.3
    inv.e_grid_out_total = 892.4
    inv.e_grid_in_total = 1234.5
    inv.v_ac1 = 240.1
    inv.f_ac1 = 50.02
    inv.p_load_demand = 1200
    # givenergy-modbus #174/#176: e_load_day was AC charge; e_inverter_out_day/total
    # are PV generation. Consumption is derived. All entity keys renamed in 2.1.1/2.
    inv.e_ac_charge_today = 3.8
    inv.e_consumption_today = 21.4
    # Self-consumption = max(0, PV generation - grid export), a SinglePhaseInverter
    # computed_field (givenergy-modbus 2.5.12). Coherent with the PV/export values
    # above (11.2 - 2.1, 5100.2 - 892.4) so the mock mirrors the real derivation.
    inv.e_self_consumption_today = 9.1
    inv.e_self_consumption_total = 4207.8
    # PV direct to load (givenergy-modbus 2.5.13, DC-coupled GEN1 only).
    inv.e_pv_direct_today = 5.3
    # Battery topology nameplate (HR308-310, givenergy-modbus 2.6.0).
    inv.battery_nominal_power = 3600
    inv.battery_nominal_current = 70
    inv.battery_max_charge_pct = 100
    # Native load registers (IR 1396-1399) exist only on three-phase models —
    # mirror the real model so the mock can't fabricate fields the sensors
    # then break on. Three-phase tests set these (and delete the derived
    # e_consumption_today) explicitly.
    del inv.e_load_today
    del inv.e_load_total
    inv.e_pv_generation_today = 11.2
    inv.e_pv_generation_total = 5100.2
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
    inv.slot_map = SINGLE_PHASE_SLOTS
    inv.charge_slot_1 = TimeSlot(start=time(0, 30), end=time(4, 30))
    inv.charge_slot_2 = TimeSlot(start=time(0, 0), end=time(0, 0))
    inv.discharge_slot_1 = TimeSlot(start=time(17, 0), end=time(22, 0))
    inv.discharge_slot_2 = TimeSlot(start=time(0, 0), end=time(0, 0))
    # Status / mode
    inv.inverter_errors = 0
    inv.charger_warning_code = 0
    inv.charge_status_label = ChargeStatus.CHARGING
    inv.system_mode = 1
    inv.battery_pause_mode = 0
    inv.battery_pause_slot_1 = TimeSlot(start=time(0, 0), end=time(0, 0))
    inv.smart_load_slot_1 = TimeSlot(start=time(6, 0), end=time(7, 0))
    for i in range(2, 11):
        setattr(inv, f"smart_load_slot_{i}", TimeSlot(start=time(0, 0), end=time(0, 0)))
    # AC output + power quality
    inv.v_ac1_output = 240.3
    inv.f_ac1_output = 50.01
    inv.i_ac1 = 5.2
    inv.p_grid_apparent = 850
    inv.pf_inverter_output_now = 0.98
    inv.p_grid_out_ph1 = -800
    # Additional energy totals
    inv.e_inverter_export_total = 2105.7
    inv.e_inverter_in_total = 312.4
    inv.e_discharge_year = 421.8
    # EPS / generation
    inv.p_backup = 0
    inv.p_combined_generation = 2500
    # Identification / firmware
    inv.device_type_code = "2001"
    inv.num_mppt = 2
    inv.num_phases = 1
    inv.arm_firmware_version = 449
    inv.dsp_firmware_version = 451
    inv.modbus_version = 16
    inv.meter_type = MeterType.CT_OR_EM418
    inv.battery_type = BatteryType.LITHIUM
    inv.battery_capacity_ah = 160
    inv.battery_capacity_kwh = 8.19
    return inv


@pytest.fixture
def mock_battery() -> MagicMock:
    bat = MagicMock()
    bat.precision_of = Battery.precision_of
    bat.serial_number = "BT1234A001"
    bat.soc = 85
    bat.v_out = 52.4
    bat.t_max = 24.5
    bat.t_min = 21.3
    bat.cap_remaining = 8.5
    bat.cap_design = 9.5
    bat.cap_calibrated = 9.4
    bat.cap_design2 = 9.5
    bat.num_cycles = 42
    bat.bms_firmware_version = 3005
    bat.usb_device_inserted = 8
    # BMS internals
    bat.num_cells = 16
    bat.v_cells_sum = 52.412
    bat.t_bms_mosfet = 28.4
    # BMS status / warning bitmaps (rendered as hex by the sensor layer).
    for i in range(1, 8):
        setattr(bat, f"status_{i}", 0)
    bat.status_3 = 0xA5  # one non-zero status to exercise the formatter
    bat.warning_1 = 0
    bat.warning_2 = 0
    for i in range(1, 17):
        setattr(bat, f"v_cell_{i:02d}", 3.275 + i * 0.001)  # ~3.276–3.291 V
    bat.t_cells_01_04 = 22.1
    bat.t_cells_05_08 = 22.4
    bat.t_cells_09_12 = 22.6
    bat.t_cells_13_16 = 22.3
    return bat


@pytest.fixture
def mock_plant(mock_inverter, mock_battery) -> MagicMock:
    plant = MagicMock()
    plant.inverter_serial_number = "SA1234G123"
    plant.data_adapter_serial_number = "WF1234G456"
    plant.inverter = mock_inverter
    plant.batteries = [mock_battery]
    plant.number_batteries = 1
    # Non-AIO by default — AIO per-module tests override this with mock modules.
    plant.aio_battery_modules = []
    # No HV battery stacks by default — HV-stack tests override this (#95).
    plant.hv_stacks = []
    # No EMS by default; the EMS-specific tests override this with a mock Ems so
    # the EMS scheduling entities are only created for EMS plants.
    plant.ems = None
    # A real PlantCapabilities — the integration's save-on-success path calls
    # .to_dict() through CapabilitiesCache, which would choke on a MagicMock.
    plant.capabilities = PlantCapabilities(
        device_type=Model.HYBRID,
        inverter_address=0x32,
        meter_addresses=[],
        lv_battery_addresses=[0x32],
        bcu_stacks=[],
    )
    return plant


@pytest.fixture
def mock_client(mock_plant) -> AsyncMock:
    # spec_set deliberately omits the deprecated refresh_plant() so any lingering
    # call to it fails fast (AttributeError) rather than silently auto-mocking —
    # guards the #125 migration against regressions.
    client = AsyncMock(
        spec_set=[
            "connected",
            "plant",
            "refresh",
            "load_config",
            "connect",
            "detect",
            "close",
            "one_shot_command",
            "capture_frames",
        ]
    )
    client.connected = True
    client.plant = mock_plant
    client.refresh = AsyncMock(return_value=mock_plant)
    client.load_config = AsyncMock(return_value=mock_plant)
    client.connect = AsyncMock()
    client.detect = AsyncMock()
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
