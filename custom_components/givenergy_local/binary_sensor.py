"""Binary sensor platform — a single plant-level battery out-of-spec alert (#78)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyUpdateCoordinator

# LFP soft operating band. A cell that has genuinely drifted outside this for a
# sustained period warrants attention; the band is deliberately wider than the
# nominal working range so normal operation never crosses it.
CELL_MIN_V = 3.0
CELL_MAX_V = 3.5
# Unused cell slots in smaller packs read ~0 V. Anything below this floor is an
# unpopulated slot or a dropped read, not a real over-discharged cell, so it is
# excluded from the low-voltage check (the debounce handles dropped reads anyway).
CELL_PRESENT_FLOOR_V = 1.0

# Default cell-group temperature alert band (°C). Conservative LFP envelope; a
# tunable knob can follow if real-world data shows it needs widening.
TEMP_MIN_C = 0.0
TEMP_MAX_C = 50.0

# Hybrid debounce: a value must be out of spec for at least this long in
# wall-clock AND across at least this many distinct polls before the alert
# trips. Both bounds comfortably exceed the ~2-minute persistence that dongle
# bad-read garbage can exhibit (modbus#78), so a sustained fake read can't trip
# it, while the dual form survives both fast and slow pollers.
DEBOUNCE_SECONDS = 300
DEBOUNCE_MIN_POLLS = 3


@dataclass
class _Offender:
    """Tracks one metric that is currently out of spec across consecutive polls."""

    battery: str
    metric: str
    value: float
    first_seen: datetime
    poll_count: int = 1


@dataclass
class _Reading:
    battery: str
    metric: str
    value: float
    detail: dict[str, object] = field(default_factory=dict)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Pointless on a battery-less (PV-only) install.
    if not coordinator.data.batteries:
        return
    async_add_entities([GivEnergyBatteryOutOfSpecBinarySensor(coordinator)])


def _iter_readings(batteries: list) -> Iterator[_Reading]:
    """Yield every in-spec-checkable cell-voltage and group-temperature reading."""
    for index, battery in enumerate(batteries):
        serial = battery.serial_number or f"battery_{index}"
        for cell in range(1, 17):
            value = getattr(battery, f"v_cell_{cell:02d}", None)
            if value is None or value < CELL_PRESENT_FLOOR_V:
                continue  # unpopulated slot or dropped read
            yield _Reading(serial, f"cell_{cell:02d}_voltage", float(value), {"cell": cell})
        for lo, hi in ((1, 4), (5, 8), (9, 12), (13, 16)):
            value = getattr(battery, f"t_cells_{lo:02d}_{hi:02d}", None)
            if value is None:
                continue
            yield _Reading(
                serial,
                f"cells_{lo:02d}_{hi:02d}_temperature",
                float(value),
                {"cell_group": f"{lo}-{hi}"},
            )


def _out_of_spec(reading: _Reading) -> bool:
    if reading.metric.endswith("_voltage"):
        return not (CELL_MIN_V <= reading.value <= CELL_MAX_V)
    return not (TEMP_MIN_C <= reading.value <= TEMP_MAX_C)


class GivEnergyBatteryOutOfSpecBinarySensor(
    CoordinatorEntity[GivEnergyUpdateCoordinator], BinarySensorEntity
):
    """One plant-level alert: on when any monitored battery value has been out of
    spec for a sustained period, debounced against transient bad reads."""

    _attr_has_entity_name = True
    _attr_name = "Battery Out Of Spec"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GivEnergyUpdateCoordinator) -> None:
        super().__init__(coordinator)
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_battery_out_of_spec"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})
        self._offenders: dict[str, _Offender] = {}
        self._last_processed_refresh: datetime | None = None
        self._evaluate()

    def _handle_coordinator_update(self) -> None:
        self._evaluate()
        super()._handle_coordinator_update()

    def _evaluate(self) -> None:
        """Advance the per-metric debounce trackers off the latest poll.

        Runs once per distinct successful refresh so each poll counts exactly
        once towards the sustained-poll requirement.
        """
        refresh = self.coordinator.last_successful_refresh
        if refresh is None or refresh == self._last_processed_refresh:
            return
        self._last_processed_refresh = refresh

        current: dict[str, _Reading] = {
            f"{r.battery}:{r.metric}": r
            for r in _iter_readings(self.coordinator.data.batteries)
            if _out_of_spec(r)
        }
        # Drop anything that has returned to spec.
        for key in list(self._offenders):
            if key not in current:
                del self._offenders[key]
        # Record/advance current offenders.
        for key, reading in current.items():
            existing = self._offenders.get(key)
            if existing is None:
                self._offenders[key] = _Offender(
                    battery=reading.battery,
                    metric=reading.metric,
                    value=reading.value,
                    first_seen=refresh,
                )
            else:
                existing.value = reading.value
                existing.poll_count += 1

    @property
    def is_on(self) -> bool:
        refresh = self.coordinator.last_successful_refresh
        if refresh is None:
            return False
        return any(
            offender.poll_count >= DEBOUNCE_MIN_POLLS
            and (refresh - offender.first_seen).total_seconds() >= DEBOUNCE_SECONDS
            for offender in self._offenders.values()
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        refresh = self.coordinator.last_successful_refresh
        offenders = [
            {
                "battery": o.battery,
                "metric": o.metric,
                "value": o.value,
                "polls_out_of_spec": o.poll_count,
                "seconds_out_of_spec": (
                    int((refresh - o.first_seen).total_seconds()) if refresh else 0
                ),
            }
            for o in self._offenders.values()
        ]
        return {"offenders": offenders}
