"""Regression guard: control-platform DeviceInfo carries the device `name`.

The select/switch/number/time entities share the inverter device but each build
their own ``DeviceInfo``. If that ``DeviceInfo`` omits ``name``, HA cannot derive
the device-name-prefixed entity_id slug when the platform happens to set up before
the named device record exists — the entity_id silently falls back to a bare slug
(e.g. ``select.battery_power_mode`` instead of
``select.givenergy_inverter_sa1234g123_battery_power_mode``), which breaks the
dashboard's entity references.

Asserting the name at construction is order-independent, unlike the probabilistic
full-setup guard in ``test_script_entity_refs`` which only trips when the async
platform-setup race happens to land the wrong way.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.givenergy_local.number import (
    EMS_NUMBER_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    GivEnergyEmsNumberEntity,
    GivEnergyNumberEntity,
)
from custom_components.givenergy_local.select import (
    SELECT_DESCRIPTIONS,
    GivEnergySelectEntity,
)
from custom_components.givenergy_local.switch import (
    EMS_SWITCH_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    GivEnergyEmsSwitchEntity,
    GivEnergySwitchEntity,
)
from custom_components.givenergy_local.time import (
    EMS_TIME_DESCRIPTIONS,
    TIME_DESCRIPTIONS,
    GivEnergyEmsTimeEntity,
    GivEnergyTimeEntity,
)


@pytest.mark.parametrize(
    ("entity_cls", "description"),
    [
        (GivEnergySelectEntity, SELECT_DESCRIPTIONS[0]),
        (GivEnergySwitchEntity, SWITCH_DESCRIPTIONS[0]),
        (GivEnergyEmsSwitchEntity, EMS_SWITCH_DESCRIPTIONS[0]),
        (GivEnergyNumberEntity, NUMBER_DESCRIPTIONS[0]),
        (GivEnergyEmsNumberEntity, EMS_NUMBER_DESCRIPTIONS[0]),
        (GivEnergyTimeEntity, TIME_DESCRIPTIONS[0]),
        (GivEnergyEmsTimeEntity, EMS_TIME_DESCRIPTIONS[0]),
    ],
)
def test_control_entity_device_info_carries_name(mock_plant, entity_cls, description):
    coordinator = MagicMock()
    coordinator.data = mock_plant
    entity = entity_cls(coordinator, description)
    assert entity.device_info["name"] == "GivEnergy Inverter SA1234G123"
