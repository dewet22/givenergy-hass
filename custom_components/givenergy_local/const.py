DOMAIN = "givenergy_local"

DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 30

CONF_SCAN_INTERVAL = "scan_interval"
CONF_PASSIVE = "passive"
# Retained only for migrating older config entries — see async_migrate_entry.
# The current defaults live as constructor defaults on GivEnergyUpdateCoordinator.
CONF_TIMEOUT_TOLERANCE = "timeout_tolerance"
CONF_RETRIES = "retries"

DEFAULT_PASSIVE = False

PLATFORMS = ["sensor", "switch", "number", "select", "time"]

SERVICE_REBOOT_INVERTER = "reboot_inverter"
SERVICE_CALIBRATE_BATTERY_SOC = "calibrate_battery_soc"
SERVICE_GENERATE_DASHBOARD = "generate_dashboard"
SERVICE_CAPTURE_FRAMES = "capture_frames"
