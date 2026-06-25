from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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

PLATFORMS = ["binary_sensor", "sensor", "switch", "number", "select", "time", "datetime"]

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


# --- Experimental features (opt-in givenergy-modbus client flags) -------------
# A grouped, collapsed "Experimental features" section in the options flow. Each
# entry forwards one optional kwarg into Client(...) when its toggle is on.
#
# Adding a feature = ONE entry below + a label in strings.json /
# translations/en.json under options.step.init.sections.experimental.data, and
# (when the kwarg ships) bumping the givenergy-modbus floor in pyproject.toml and
# manifest.json. No version-guard code is needed: the pin bump is committed
# together with the kwarg-passing entry, so any build that can pass the kwarg
# already depends on a client that accepts it.
#
# Worked example (uncomment + set the real kwarg name when the modbus release
# lands; client_value matches the kwarg's type — e.g. a float for a tunable):
#   EXPERIMENTAL_FEATURES = (
#       ExperimentalFeature(
#           conf_key="splice_heal",
#           client_kwarg="splice_heal_seconds",
#           client_value=5.0,
#       ),
#   )
CONF_EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class ExperimentalFeature:
    """One opt-in client flag. The UI toggle is boolean; `client_value` is what
    gets passed to Client(...) when the toggle is on (True for a bool flag, or a
    concrete value like a float for a tunable)."""

    conf_key: str
    client_kwarg: str
    client_value: Any = True
    default: bool = False  # MUST stay False — enforced by test.


EXPERIMENTAL_FEATURES: tuple[ExperimentalFeature, ...] = ()


def resolve_experimental_client_kwargs(
    options: Mapping[str, Any],
    features: tuple[ExperimentalFeature, ...] = EXPERIMENTAL_FEATURES,
) -> dict[str, Any]:
    """Map enabled experimental toggles in `options` to Client(...) kwargs.

    Reads the nested section dict (options[CONF_EXPERIMENTAL]); returns {} when
    nothing is enabled, so an all-off entry constructs Client() identically to
    before this mechanism existed. Unknown section keys (e.g. a removed feature)
    are ignored.
    """
    section = options.get(CONF_EXPERIMENTAL, {}) or {}
    return {
        feature.client_kwarg: feature.client_value
        for feature in features
        if section.get(feature.conf_key, feature.default)
    }
