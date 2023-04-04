"""Constants for the GivEnergy integration."""
from enum import Enum
from logging import Logger, getLogger

LOGGER = getLogger(__package__)

NAME = "GivEnergy"
DOMAIN = "givenergy"
VERSION = "0.0.1"
MANUFACTURER = "GivEnergy"

CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_FULL_REFRESH_INTERVAL = "full_refresh_interval"

class Icon(str, Enum):
    """Icon styles."""

    SOLAR = "mdi:solar-power-variant"
    LOAD = "mdi:power-socket-uk"
    BATTERY = "mdi:battery-high"
    BATTERY_CYCLES = "mdi:battery-sync"
    BATTERY_TEMPERATURE = "mdi:thermometer"
    BATTERY_MINUS = "mdi:battery-minus-variant"
    BATTERY_PLUS = "mdi:battery-plus-variant"
    INVERTER = "mdi:home-lightning-bolt"
    GRID_IMPORT = "mdi:transmission-tower-export"
    GRID_EXPORT = "mdi:transmission-tower-import"
    EPS = "mdi:power-plug-battery"
    TEMPERATURE = "mdi:thermometer"
