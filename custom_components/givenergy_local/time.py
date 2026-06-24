from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time as dt_time

from givenergy_modbus.client import commands
from givenergy_modbus.model import TimeSlot
from givenergy_modbus.model.ems import Ems
from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BATTERY_DATA_ONLY, DOMAIN
from .coordinator import GivEnergyUpdateCoordinator, InverterModel
from .sensor import _device_kind

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GivEnergyTimeEntityDescription(TimeEntityDescription):
    slot_fn: Callable[[InverterModel], TimeSlot | None] = field(default=lambda _: None)
    is_start: bool = True
    # Writes just the relevant endpoint register. Takes the new value and the
    # current inverter — charge/discharge setters dispatch on inverter.slot_map
    # to handle extended-slot models; the pause setter ignores the inverter.
    setter_fn: Callable[[dt_time, InverterModel], list] = field(default=lambda _, __: [])
    # If True, the control isn't created when its readability signal reads None at
    # setup — the register isn't present on this device/firmware (e.g. the battery
    # pause slot, firmware-gated). The control-side of the sensor skip_if_none (#207).
    skip_if_none: bool = False
    # The readability signal for skip_if_none. A decoded TimeSlot is NOT a presence
    # signal — it's None for a valid-but-unset slot (raw-60 sentinel, shown as
    # "--:--") as well as for an absent register — so gate on a separate register
    # (e.g. the pause-mode register as a proxy for the pause feature) rather than
    # slot_fn (#208 review). Defaults to slot_fn when unset.
    readable_fn: Callable[[InverterModel], object] | None = None


TIME_DESCRIPTIONS: tuple[GivEnergyTimeEntityDescription, ...] = (
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_start",
        name="Charge Slot 1 Start",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_charge_slot_start(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_1_end",
        name="Charge Slot 1 End",
        slot_fn=lambda inv: inv.charge_slot_1,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_charge_slot_end(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_start",
        name="Charge Slot 2 Start",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_charge_slot_start(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="charge_slot_2_end",
        name="Charge Slot 2 End",
        slot_fn=lambda inv: inv.charge_slot_2,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_charge_slot_end(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_start",
        name="Discharge Slot 1 Start",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_discharge_slot_start(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_1_end",
        name="Discharge Slot 1 End",
        slot_fn=lambda inv: inv.discharge_slot_1,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_discharge_slot_end(1, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_start",
        name="Discharge Slot 2 Start",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=True,
        setter_fn=lambda value, inv: commands.set_discharge_slot_start(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="discharge_slot_2_end",
        name="Discharge Slot 2 End",
        slot_fn=lambda inv: inv.discharge_slot_2,
        is_start=False,
        setter_fn=lambda value, inv: commands.set_discharge_slot_end(2, value, inv.slot_map),
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="battery_pause_slot_start",
        name="Battery Pause Slot Start",
        slot_fn=lambda inv: inv.battery_pause_slot_1,
        is_start=True,
        setter_fn=lambda value, _inv: commands.set_pause_slot_start(value),
        skip_if_none=True,  # firmware-gated (#207)
        # Gate on the pause-mode register's presence, not the slot: an unset slot
        # also decodes to None, but the control must stay so it can be set (#208).
        readable_fn=lambda inv: inv.battery_pause_mode,
        entity_category=EntityCategory.CONFIG,
    ),
    GivEnergyTimeEntityDescription(
        key="battery_pause_slot_end",
        name="Battery Pause Slot End",
        slot_fn=lambda inv: inv.battery_pause_slot_1,
        is_start=False,
        setter_fn=lambda value, _inv: commands.set_pause_slot_end(value),
        skip_if_none=True,  # firmware-gated (#207)
        # Gate on the pause-mode register's presence, not the slot: an unset slot
        # also decodes to None, but the control must stay so it can be set (#208).
        readable_fn=lambda inv: inv.battery_pause_mode,
        entity_category=EntityCategory.CONFIG,
    ),
)


# --- EMS plant-level slots (only created for EMS plants) ---


@dataclass(frozen=True, kw_only=True)
class GivEnergyEmsTimeEntityDescription(TimeEntityDescription):
    slot_fn: Callable[[Ems], TimeSlot | None] = field(default=lambda _: None)
    is_start: bool = True
    # EMS setters bake in the slot index and write just the relevant endpoint;
    # unlike the inverter setters they need no slot_map.
    setter_fn: Callable[[dt_time], list] = field(default=lambda _: [])


def _slot_getter(attr: str) -> Callable[[Ems], TimeSlot | None]:
    return lambda ems: getattr(ems, attr)


def _endpoint_setter(cmd: Callable[[int, dt_time], list], idx: int) -> Callable[[dt_time], list]:
    return lambda value: cmd(idx, value)


def _smart_load_slot_getter(idx: int) -> Callable[[InverterModel], TimeSlot | None]:
    # Both single- and three-phase models define smart_load_slot_* as optional
    # pydantic fields (default None), so direct access is safe today. The getattr
    # default is cheap insurance: these entities are created unconditionally, so a
    # future model that drops the field reads as None (entity unavailable) instead
    # of raising AttributeError.
    return lambda inv: getattr(inv, f"smart_load_slot_{idx}", None)


def _smart_load_slot_setter(
    cmd: Callable[[int, dt_time | None], list], idx: int
) -> Callable[[dt_time, InverterModel], list]:
    return lambda value, _inv: cmd(idx, value)


def _smart_load_time_descriptions() -> tuple[GivEnergyTimeEntityDescription, ...]:
    """Start/end time entities for Smart Load slots 1–10 (HR 554–573)."""
    descriptions: list[GivEnergyTimeEntityDescription] = []
    for idx in range(1, 11):
        for endpoint, is_start, cmd in (
            ("start", True, commands.set_smart_load_slot_start),
            ("end", False, commands.set_smart_load_slot_end),
        ):
            descriptions.append(
                GivEnergyTimeEntityDescription(
                    key=f"smart_load_slot_{idx}_{endpoint}",
                    name=f"Smart Load Slot {idx} {endpoint.title()}",
                    slot_fn=_smart_load_slot_getter(idx),
                    is_start=is_start,
                    setter_fn=_smart_load_slot_setter(cmd, idx),
                    entity_category=EntityCategory.CONFIG,
                )
            )
    return tuple(descriptions)


SMART_LOAD_TIME_DESCRIPTIONS: tuple[GivEnergyTimeEntityDescription, ...] = (
    _smart_load_time_descriptions()
)


def _ems_time_descriptions() -> tuple[GivEnergyEmsTimeEntityDescription, ...]:
    """Start/end time entities for EMS charge, discharge & export slots 1-3."""
    descriptions: list[GivEnergyEmsTimeEntityDescription] = []
    for kind in ("charge", "discharge", "export"):
        endpoints = (
            ("start", True, getattr(commands, f"set_ems_{kind}_slot_start")),
            ("end", False, getattr(commands, f"set_ems_{kind}_slot_end")),
        )
        for idx in (1, 2, 3):
            for endpoint, is_start, cmd in endpoints:
                descriptions.append(
                    GivEnergyEmsTimeEntityDescription(
                        key=f"ems_{kind}_slot_{idx}_{endpoint}",
                        name=f"EMS {kind.title()} Slot {idx} {endpoint.title()}",
                        slot_fn=_slot_getter(f"{kind}_slot_{idx}"),
                        is_start=is_start,
                        setter_fn=_endpoint_setter(cmd, idx),
                        entity_category=EntityCategory.CONFIG,
                    )
                )
    return tuple(descriptions)


EMS_TIME_DESCRIPTIONS: tuple[GivEnergyEmsTimeEntityDescription, ...] = _ems_time_descriptions()


def _include_time(description: GivEnergyTimeEntityDescription, inverter: InverterModel) -> bool:
    """Whether to create a time control at setup (#207).

    A skip_if_none control is dropped when its readability signal reads None —
    absent on this device/firmware. The decoded slot is NOT that signal (it's None
    for a valid-but-unset slot too), so use readable_fn when given (#208 review).
    Guarded so a raising accessor skips just that control, not the whole platform.
    """
    if not description.skip_if_none:
        return True
    read = description.readable_fn or description.slot_fn
    try:
        value = read(inverter)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Skipping time %s: readability check raised at setup", description.key)
        return False
    if value is None:
        # Expected: the register isn't present on this device/firmware. DEBUG, not
        # WARNING — the gate working as designed, recurring every restart, unlike the
        # duplicate/garbled anomalies that warrant a warning.
        _LOGGER.debug("Skipping time %s: readability signal reads None at setup", description.key)
        return False
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GivEnergyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.options.get(CONF_BATTERY_DATA_ONLY, False):
        # Battery-data-only (#95): this unit's controls are owned by its Gateway.
        return
    entities: list[TimeEntity] = []
    if coordinator.data.ems is not None:
        # EMS plant: the controller owns scheduling. Expose its slots and skip the
        # inverter-level charge/discharge and Smart Load slots — redundant when the
        # EMS is authoritative, and the library only populates the inverter slot
        # registers (incl. HR554-573) on non-EMS inverters, so they'd be a block of
        # permanently-unavailable config entities with silent-no-op writes (#201).
        entities.extend(
            GivEnergyEmsTimeEntity(coordinator, description)
            for description in EMS_TIME_DESCRIPTIONS
        )
    else:
        inverter = coordinator.data.inverter
        entities.extend(
            GivEnergyTimeEntity(coordinator, description)
            for description in TIME_DESCRIPTIONS
            if _include_time(description, inverter)
        )
        entities.extend(
            GivEnergyTimeEntity(coordinator, description)
            for description in SMART_LOAD_TIME_DESCRIPTIONS
        )
    async_add_entities(entities)


class GivEnergyTimeEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], TimeEntity):
    _attr_has_entity_name = True
    entity_description: GivEnergyTimeEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyTimeEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )

    @property
    def native_value(self) -> dt_time | None:
        slot = self.entity_description.slot_fn(self.coordinator.data.inverter)
        if slot is None:
            return None
        return slot.start if self.entity_description.is_start else slot.end

    async def async_set_value(self, value: dt_time) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        inverter = self.coordinator.data.inverter
        if self.entity_description.slot_fn(inverter) is None:
            return
        await client.one_shot_command(self.entity_description.setter_fn(value, inverter))
        await self.coordinator.async_request_refresh()


class GivEnergyEmsTimeEntity(CoordinatorEntity[GivEnergyUpdateCoordinator], TimeEntity):
    """Start/end time control for an EMS plant-level charge/discharge slot."""

    _attr_has_entity_name = True
    entity_description: GivEnergyEmsTimeEntityDescription

    def __init__(
        self,
        coordinator: GivEnergyUpdateCoordinator,
        description: GivEnergyEmsTimeEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        serial = coordinator.data.inverter_serial_number
        self._attr_unique_id = f"{serial}_{description.key}"
        model = coordinator.data.inverter.model
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivEnergy {_device_kind(model)} {serial}",
        )

    @property
    def native_value(self) -> dt_time | None:
        ems = self.coordinator.data.ems
        if ems is None:
            return None
        slot = self.entity_description.slot_fn(ems)
        if slot is None:
            return None
        return slot.start if self.entity_description.is_start else slot.end

    async def async_set_value(self, value: dt_time) -> None:
        client = self.coordinator._client
        if client is None or not client.connected:
            return
        await client.one_shot_command(self.entity_description.setter_fn(value))
        await self.coordinator.async_request_refresh()
