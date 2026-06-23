DOMAIN = "givenergy_local"

DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 30

CONF_SCAN_INTERVAL = "scan_interval"
CONF_PASSIVE = "passive"
# When enabled (per-entry option), suppress control entities and inverter-level
# system sensors, leaving only battery pack / HV stack / AIO module / diagnostic
# data. For a unit controlled by a Gateway in a parallel group, where its own
# per-unit controls and derived consumption figures are misleading (#95).
CONF_BATTERY_DATA_ONLY = "battery_data_only"
DEFAULT_BATTERY_DATA_ONLY = False
# Retained only for migrating older config entries — see async_migrate_entry.
# The current defaults live as constructor defaults on GivEnergyUpdateCoordinator.
CONF_TIMEOUT_TOLERANCE = "timeout_tolerance"
CONF_RETRIES = "retries"

DEFAULT_PASSIVE = False

PLATFORMS = ["binary_sensor", "sensor", "switch", "number", "select", "time"]

SERVICE_REBOOT_INVERTER = "reboot_inverter"
SERVICE_CALIBRATE_BATTERY_SOC = "calibrate_battery_soc"
SERVICE_CAPTURE_FRAMES = "capture_frames"
SERVICE_REDETECT_PLANT = "redetect_plant"
SERVICE_EXPOSE_RECOMMENDED_ENTITIES = "expose_recommended_entities"
SERVICE_SET_SYSTEM_DATETIME = "set_system_datetime"

# Curated headline entities for the expose_recommended_entities service.
# Each value is an entity-description `key` (the suffix portion of unique_id).
# Topology variation is handled implicitly: keys with no corresponding entity
# for a given entry (e.g. battery_* on PV-only installs) are silently skipped.
EXPOSE_RECOMMENDED_ENTITY_KEYS = (
    # PV
    "p_pv",
    "e_pv_day",
    "e_pv_total",
    # Battery (auto-skipped on PV-only installs)
    "battery_soc",
    "p_battery",
    "e_battery_charge_day",
    "e_battery_discharge_day",
    "e_battery_throughput",
    # Grid
    "grid_power",
    "e_grid_in_day",
    "e_grid_out_day",
    "e_grid_in_total",
    "e_grid_out_total",
    # Load / Consumption
    "p_load_demand",
    "e_consumption_today",
    "e_load_total",  # three-phase only — silently skipped elsewhere
    # PV generation total (was e_inverter_out_total / "Inverter Output Total")
    "e_pv_generation_total",
    # Health — entity description's `key` is "status"; `inverter_status` is
    # the translation_key, which is not what's in the unique_id suffix.
    "status",
)
