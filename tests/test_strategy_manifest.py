"""Parity guard: the JS dashboard strategy resolves entities by their integration
`key` (unique_id = ``{serial}_{key}``). This test pins every key the strategy
references to a real ``EntityDescription.key``, so renaming a key in a platform
file fails loudly here instead of silently dangling a card — the same discipline
``test_script_entity_refs.py`` applies to the Python YAML generator.

Static ``a.inv("key")`` / ``a.bat(rec, "key")`` literals are scraped straight
from ``ge-strategy.js``; the handful of dynamically-built key families (cells,
BMS status, smart-load and EMS slots) are enumerated explicitly below.
"""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.givenergy_local import (
    binary_sensor,
    number,
    select,
    sensor,
    switch,
)
from custom_components.givenergy_local import (
    time as ge_time,
)

_STRATEGY_JS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "givenergy_local"
    / "www"
    / "ge-strategy.js"
)

# Literal keys referenced as a.inv("...") or a.bat(rec, "..."). Dynamic refs
# (string concatenation) are intentionally not matched here — see _DYNAMIC_KEYS.
_LITERAL_REF = re.compile(r'\.(?:inv|bat)\((?:rec, )?"([a-z0-9_]+)"\)')

# Key families the strategy builds at runtime via string concatenation.
_DYNAMIC_KEYS: set[str] = set()
for _c in range(1, 17):
    _DYNAMIC_KEYS.add(f"v_cell_{_c:02d}")
for _s in range(1, 8):
    _DYNAMIC_KEYS.add(f"status_{_s}")
for _i in range(1, 11):
    _DYNAMIC_KEYS.add(f"smart_load_slot_{_i}_start")
    _DYNAMIC_KEYS.add(f"smart_load_slot_{_i}_end")
for _kind in ("charge", "discharge", "export"):
    for _i in range(1, 4):
        _DYNAMIC_KEYS.add(f"ems_{_kind}_slot_{_i}_start")
        _DYNAMIC_KEYS.add(f"ems_{_kind}_slot_{_i}_end")
        _DYNAMIC_KEYS.add(f"ems_{_kind}_target_soc_{_i}")


# Some entities (e.g. the battery-out-of-spec binary sensor) build a hardcoded
# unique_id suffix rather than carrying an EntityDescription; pick those up too.
_HARDCODED_UID = re.compile(r'unique_id\s*=\s*f"\{serial\}_([a-z0-9_]+)"')


def _real_keys() -> set[str]:
    """Every key the integration can register as a ``{serial}_{key}`` unique_id."""
    keys: set[str] = set()
    for module in (sensor, binary_sensor, number, select, switch, ge_time):
        for value in vars(module).values():
            if isinstance(value, tuple) and value and hasattr(value[0], "key"):
                keys.update(d.key for d in value)
        source = Path(module.__file__).read_text()
        keys.update(_HARDCODED_UID.findall(source))
    return keys


def _strategy_keys() -> set[str]:
    text = _STRATEGY_JS.read_text()
    return set(_LITERAL_REF.findall(text)) | _DYNAMIC_KEYS


def test_strategy_keys_are_all_real_entity_keys() -> None:
    real = _real_keys()
    referenced = _strategy_keys()
    unknown = sorted(k for k in referenced if k not in real)
    assert not unknown, (
        "ge-strategy.js references keys with no matching EntityDescription.key "
        f"(renamed or removed?): {unknown}"
    )


def test_strategy_literal_scrape_found_references() -> None:
    # Guard against the regex silently matching nothing (e.g. if the accessor
    # naming changes), which would make the parity test vacuously pass.
    literals = set(_LITERAL_REF.findall(_STRATEGY_JS.read_text()))
    assert len(literals) > 40, f"expected many literal key refs, found {len(literals)}"
