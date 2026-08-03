"""Generate a Predbat ``apps.yaml`` starting template from the live registry (#289).

Predbat needs an ``apps.yaml`` wiring its fields to Home Assistant entities. Hand-
building that is fiddly and drifts whenever an entity is renamed (entity IDs derive
from display names). Instead we resolve each field from the entity registry by the
entity's stable ``unique_id`` (``{serial}_{key}``) to its *current* ``entity_id`` —
so the output is per-install correct and rename-proof.

Scope (v1): single-inverter hybrid, single-inverter AC-coupled, and EMS. Other
topologies (multi-inverter non-EMS, three-phase, Gateway) get a short note.

The EMS control surface is the EMS *controller* only — modbus confirmed (note
1783973795) the managed inverters are telemetry-only summaries and the controller
has no charge-rate or SOC-reserve register — so those Predbat fields are omitted
with a comment on an EMS plant rather than emitted as dead entities.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DOMAIN = "givenergy_local"

PREDBAT_DOCS_URL = "https://springfall2008.github.io/batpred/"

# `entities` / `devices` are duck-typed: HA registry entries (RegistryEntry /
# DeviceEntry) in production, lightweight namespaces in tests. We read
# entity_id / platform / device_id / unique_id / disabled_by off entities and
# id / identifiers / via_device_id off devices.

# --- topology -------------------------------------------------------------------


# A device's kind, inferred from its registered key set (never from its name, which
# the user can change). Mirrors the dashboard strategy's classify().
def _classify(keys: set[str]) -> str:
    if "ems_plant_enable" in keys:
        return "ems"
    if "v_cell_01" in keys or "num_cycles" in keys:
        return "battery"
    if "p_pv" in keys:
        return "inverter"
    if "soc" in keys:
        return "battery"
    return "other"


class _Plant:
    """Resolved plant: the control-authority device + battery/inverter roles."""

    def __init__(self) -> None:
        self.ems: tuple[str, dict[str, str]] | None = None  # (serial, key->entity_id)
        # inverters carry (serial, enabled key->entity_id, full key set incl. disabled).
        # The full set drives topology decisions (e.g. AC-coupled), so disabling an
        # entity can't silently reclassify the plant — mirrors _classify.
        self.inverters: list[tuple[str, dict[str, str], set[str]]] = []
        self.batteries: list[tuple[str, dict[str, str]]] = []

    @property
    def is_ems(self) -> bool:
        return self.ems is not None


def _build_plant(entities: Iterable[Any], devices: Iterable[Any]) -> _Plant:
    # deviceId -> serial, skipping EMS managed-inverter summary devices (their
    # identifier is `{serial}_managed`; they carry no controls, telemetry only).
    serial_by_device: dict[str, str] = {}
    for dev in devices:
        ident = next((v for (d, v) in dev.identifiers if d == DOMAIN), None)
        if ident is None or ident.endswith("_managed"):
            continue
        serial_by_device[dev.id] = ident

    # deviceId -> {key: entity_id} for enabled givenergy entities, plus the full
    # key set (incl. disabled) for classification.
    key_map: dict[str, dict[str, str]] = {}
    all_keys: dict[str, set[str]] = {}
    for ent in entities:
        if ent.platform != DOMAIN or not ent.unique_id or ent.device_id is None:
            continue
        serial = serial_by_device.get(ent.device_id)
        if serial is None:
            continue
        prefix = f"{serial}_"
        if not ent.unique_id.startswith(prefix):
            continue
        key = ent.unique_id[len(prefix) :]
        all_keys.setdefault(ent.device_id, set()).add(key)
        if ent.disabled_by is None:
            key_map.setdefault(ent.device_id, {})[key] = ent.entity_id

    plant = _Plant()
    for device_id, serial in sorted(serial_by_device.items(), key=lambda kv: kv[1]):
        kind = _classify(all_keys.get(device_id, set()))
        keys = key_map.get(device_id, {})
        if kind == "ems":
            plant.ems = (serial, keys)
        elif kind == "inverter":
            plant.inverters.append((serial, keys, all_keys.get(device_id, set())))
        elif kind == "battery":
            plant.batteries.append((serial, keys))
    return plant


# --- mappings (Predbat field -> integration key) --------------------------------

# Single-inverter (hybrid / AC-coupled). Resolved against the inverter's keys, with
# the battery device's keys merged in for soc. AC-coupled rate maps to the AC pair
# (HR313/314); on a pure hybrid that key is absent so the line auto-omits (its rate
# control is the HR111/112 C-rate — a #281 follow-up). PV wires on both topologies —
# an AC-coupled unit meters generation AC-side rather than from a DC string (#281).
_SINGLE_INVERTER_MAP: tuple[tuple[str, str], ...] = (
    ("soc_percent", "battery_soc"),
    ("soc_kw", "soc_kwh"),
    ("soc_max", "battery_capacity_kwh"),
    ("reserve", "battery_soc_reserve"),
    ("charge_start_time", "charge_slot_1_start"),
    ("charge_end_time", "charge_slot_1_end"),
    ("charge_limit", "charge_target_soc"),
    ("discharge_start_time", "discharge_slot_1_start"),
    ("discharge_end_time", "discharge_slot_1_end"),
    ("charge_rate_percent", "battery_charge_limit_ac"),
    ("discharge_rate_percent", "battery_discharge_limit_ac"),
    ("inverter_time", "system_time"),
    ("battery_power", "p_battery"),
    ("pv_power", "p_pv"),
    ("grid_power", "grid_power"),
    ("load_power", "p_load_demand"),
    ("load_today", "e_consumption_today"),
    ("import_today", "e_grid_in_day"),
    ("export_today", "e_grid_out_day"),
    ("pv_today", "e_pv_day"),
)

# EMS: controls + telemetry all from the controller device (single control
# authority). soc energy comes from the EMS remaining-energy aggregate.
_EMS_MAP: tuple[tuple[str, str], ...] = (
    ("soc_percent", "battery_soc"),
    ("soc_kw", "ems_remaining_battery_energy"),
    ("soc_max", "battery_capacity_kwh"),
    ("charge_start_time", "ems_charge_slot_1_start"),
    ("charge_end_time", "ems_charge_slot_1_end"),
    ("charge_limit", "ems_charge_target_soc_1"),
    ("discharge_start_time", "ems_export_slot_1_start"),
    ("discharge_end_time", "ems_export_slot_1_end"),
    ("battery_power", "ems_total_battery_power"),
    ("pv_power", "p_pv"),
    ("grid_power", "ems_grid_meter_power"),
    ("load_power", "ems_calc_load_power"),
    ("load_today", "ems_calc_load_energy_today"),
    ("import_today", "e_grid_in_day"),
    ("export_today", "e_grid_out_day"),
    ("pv_today", "e_pv_day"),
)

# Predbat fields with no register on the EMS controller (modbus note 1783973795):
# emitted as an explanatory comment, never a dead entity.
_EMS_UNMAPPABLE: dict[str, str] = {
    "reserve": "the EMS controller has no SOC-reserve register",
    "charge_rate_percent": "the EMS controller has no charge-rate register",
    "discharge_rate_percent": "the EMS controller has no discharge-rate register",
    "inverter_time": "the EMS clock is not locally settable",
}


# --- rendering ------------------------------------------------------------------

_BOILERPLATE = """\
pred_bat:
  module: predbat
  class: PredBat
  prefix: predbat
  timezone: Europe/London
  currency_symbols: ["£", "p"]
  threads: auto
"""


_NOT_FOUND = "entity not found on this install — check Developer Tools > States"


def _field(name: str, entity_id: str | None, *, reason: str | None = None) -> str:
    if entity_id:
        return f"  {name}:\n  - {entity_id}"
    return f"  # {name}: {reason or _NOT_FOUND}"


def _lines_for(
    mapping: tuple[tuple[str, str], ...],
    resolved: dict[str, str],
    unmappable: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for field, key in mapping:
        out.append(_field(field, resolved.get(key)))
    for field, reason in unmappable.items():
        out.append(_field(field, None, reason=reason))
    return out


def _header(topology: str) -> str:
    lines = [
        "# Predbat apps.yaml — generated by GivEnergy Local for THIS install.",
        "# Entity IDs, serials and area are resolved from your live registry, so they're",
        "# correct as of now and survive entity renames. Regenerate any time from",
        '# Developer Tools > Actions > "GivEnergy Local: Generate Predbat Config".',
        "#",
        "# Tariff, rates, solcast and other Predbat settings are yours to complete —",
        f"# see {PREDBAT_DOCS_URL}",
    ]
    if topology == "ems":
        lines += [
            "#",
            "# IMPORTANT (EMS + Predbat): in the GivEnergy portal, set EACH inverter's own",
            "# AC-charge slot 1 and DC-discharge slot 1 to 00:00-23:59, or the inverters",
            "# ignore EMS charge commands that fall outside their own slot windows.",
        ]
    return "\n".join(lines)


def _ac_coupled(inv_all_keys: set[str]) -> bool:
    # From the register's presence, not whether the user disabled the entity —
    # mirrors _classify's use of the full key set for the same robustness.
    return "battery_charge_limit_ac" in inv_all_keys


# Hybrid rate control is the HR111/112 C-rate, which we don't wire yet (#281) — a
# permanent omission, so it gets an explanatory comment rather than "not found".
_HYBRID_RATE_UNMAPPABLE: dict[str, str] = {
    "charge_rate_percent": "hybrid rate is the HR111/112 C-rate — not wired yet (#281)",
    "discharge_rate_percent": "hybrid rate is the HR111/112 C-rate — not wired yet (#281)",
}


def generate_apps_yaml(
    entities: Iterable[Any],
    devices: Iterable[Any],
) -> str:
    """Return a Predbat ``apps.yaml`` starting template for the resolved plant."""
    plant = _build_plant(entities, devices)

    if plant.is_ems:
        assert plant.ems is not None
        _serial, ems_keys = plant.ems
        body = "\n".join(
            [
                _header("ems"),
                "",
                _BOILERPLATE + "  num_inverters: 1",
                "  # EMS plant: Predbat's GivEnergy EMS mode.",
                "  inverter_type: ['GEE']",
                "",
                *_lines_for(_EMS_MAP, ems_keys, _EMS_UNMAPPABLE),
                "",
                "  # Rated AC output of the plant in W — set to your total inverter capacity.",
                "  inverter_limit: [6000]",
            ]
        )
        return body + "\n"

    if len(plant.inverters) == 1 and not plant.batteries and not plant.inverters[0][1]:
        return _unsupported_note()
    if len(plant.inverters) == 1:
        _serial, inv_keys, inv_all_keys = plant.inverters[0]
        merged = dict(inv_keys)
        for _bat_serial, bat_keys in plant.batteries:
            for key, eid in bat_keys.items():
                merged.setdefault(key, eid)
        # soc_percent maps to the inverter's battery_soc; fall back to the battery
        # device's own soc if the inverter one is disabled/absent (the "battery keys
        # merged in for soc" the module comment promises).
        if "battery_soc" not in merged and "soc" in merged:
            merged["battery_soc"] = merged["soc"]
        ac = _ac_coupled(inv_all_keys)
        mapping = _SINGLE_INVERTER_MAP
        pv_note: list[str] = []
        unmappable: dict[str, str] = {}
        if ac:
            # PV IS wired on AC-coupled, contrary to the original assumption (#281).
            # These units have no DC string, but they do meter generation on the AC
            # side and report it through the PV registers: across the fixture corpus
            # v_pv1 tracks v_ac1 to within a volt on every AC/AIO unit (243.1 vs
            # 242.7, 245.7 vs 245.7) while a genuine DC hybrid reads an independent
            # 325-372 V, and their lifetime e_pv_total figures are large and real.
            # Confirmed live on a GIV-AC3.0 (hass#281): 1355 W at 244.0 V / 5.5 A.
            # An install with no generation CT fitted reads zero — hence the caveat
            # rather than an unconditional wiring.
            pv_note = [
                "  # NB: this inverter has no DC PV string — the figures above are generation",
                "  # metered on the AC side, which is why the reported string voltage tracks",
                "  # your mains voltage. They are real; sanity-check them against your solar",
                "  # inverter once. If they read zero, no generation CT is fitted on your",
                "  # install — point pv_today/pv_power at your separate PV inverter instead.",
            ]
        else:
            # Hybrid rate is the HR111/112 C-rate — omit with an explanatory comment
            # rather than the generic "not found" (it's never going to be there yet).
            mapping = tuple(
                (f, k)
                for (f, k) in _SINGLE_INVERTER_MAP
                if f not in ("charge_rate_percent", "discharge_rate_percent")
            )
            unmappable = _HYBRID_RATE_UNMAPPABLE
        lines = _lines_for(mapping, merged, unmappable)
        body = "\n".join(
            [
                _header("single"),
                "",
                _BOILERPLATE + "  num_inverters: 1",
                "  # No native Predbat mode for a non-EMS GivEnergy Local install yet (#287);",
                "  # start by templating GE mode onto these entities.",
                "  inverter_type: ['GE']",
                "",
                *lines,
                *pv_note,
                "",
                "  # Rated AC output of your inverter in W (e.g. 3000, 3680, 5000).",
                "  inverter_limit: [5000]",
            ]
        )
        return body + "\n"

    return _unsupported_note()


def _unsupported_note() -> str:
    return (
        "# No supported single-plant topology found.\n"
        "#\n"
        "# The generator templates single-inverter hybrid, single-inverter AC-coupled,\n"
        "# and EMS plants. Multi-inverter (non-EMS), three-phase and Gateway plants are\n"
        f"# not templated yet — please build apps.yaml by hand for now (see {PREDBAT_DOCS_URL})\n"
        "# or open an issue so we can add your topology.\n"
    )
