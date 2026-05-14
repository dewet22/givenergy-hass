DOMAIN = "givenergy_local"

DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 30

CONF_SCAN_INTERVAL = "scan_interval"
CONF_PASSIVE = "passive"
CONF_TIMEOUT_TOLERANCE = "timeout_tolerance"

DEFAULT_PASSIVE = False
DEFAULT_TIMEOUT_TOLERANCE = 5

PLATFORMS = ["sensor", "switch", "number", "select", "time"]

SERVICE_REBOOT_INVERTER = "reboot_inverter"
SERVICE_CALIBRATE_BATTERY_SOC = "calibrate_battery_soc"
