"""Tests for the generated dashboard YAML, focused on the Battery Health view."""

import yaml

from custom_components.givenergy_local.dashboard import (
    DASHBOARD_VERSION,
    generate_dashboard,
)

INV = "sa2114g047"
BATS = ["bg2134g007", "dz2228g532"]


def _views(inv: str = INV, bats: list[str] | None = None) -> dict[str, dict]:
    """Generate, parse, and index the dashboard's views by title."""
    out = generate_dashboard(inv, bats if bats is not None else BATS)
    doc = yaml.safe_load(out)  # also asserts the YAML is well-formed
    return {v["title"]: v for v in doc["views"]}


def _health_cards(bats: list[str] | None = None) -> list[dict]:
    view = _views(bats=bats)["Battery Health"]
    return view["sections"][0]["cards"]


def test_dashboard_is_valid_yaml_with_expected_views():
    views = _views()
    assert "Battery Health" in views
    # Battery Health sits between Batteries and Controls.
    titles = list(views)
    assert titles.index("Battery Health") == titles.index("Batteries") + 1


def test_dashboard_version_is_current():
    assert DASHBOARD_VERSION == 4


def test_battery_health_is_full_width_sections():
    view = _views()["Battery Health"]
    assert view["type"] == "sections"
    cards = view["sections"][0]["cards"]
    assert [c["type"] for c in cards] == [
        "markdown",
        "custom:ge-cell-heatmap",
        "custom:apexcharts-card",
        "custom:apexcharts-card",
    ]
    assert all(c["grid_options"] == {"columns": "full"} for c in cards)


def test_heatmap_lists_all_battery_serials():
    heatmap = _health_cards()[1]
    assert heatmap["batteries"] == BATS


def test_series_scale_with_battery_count():
    for bats in (["bg2134g007"], BATS, ["a1", "b2", "c3"]):
        cards = _health_cards(bats=bats)
        cell_chart, power_chart = cards[2], cards[3]
        n = len(bats)
        volt = [s for s in cell_chart["series"] if s["yaxis_id"] == "v"]
        temp = [s for s in cell_chart["series"] if s["yaxis_id"] == "temp"]
        assert len(volt) == 16 * n
        assert len(temp) == 4 * n
        # power series is 1 inverter power + one SoC per pack
        assert len(power_chart["series"]) == 1 + n


def test_health_series_reference_correct_entities():
    cards = _health_cards()
    cell_chart, power_chart = cards[2], cards[3]
    entities = {s["entity"] for s in cell_chart["series"]}
    assert "sensor.givenergy_battery_bg2134g007_cell_1_voltage" in entities
    assert "sensor.givenergy_battery_dz2228g532_cells_13_16_temperature" in entities
    power_entities = {s["entity"] for s in power_chart["series"]}
    assert f"sensor.givenergy_inverter_{INV}_battery_power" in power_entities
    assert "sensor.givenergy_battery_dz2228g532_soc" in power_entities


def test_packs_get_distinct_colours():
    cards = _health_cards()
    volt = [s for s in cards[2]["series"] if s["yaxis_id"] == "v"]
    bg_colour = next(s["color"] for s in volt if s["entity"].count("bg2134g007"))
    dz_colour = next(s["color"] for s in volt if s["entity"].count("dz2228g532"))
    assert bg_colour != dz_colour


def test_same_model_packs_get_distinct_series_labels():
    # two packs sharing a model prefix ("BG") must not collide in hover labels
    cards = _health_cards(bats=["bg1111a001", "bg2222a002"])
    names = [s["name"] for s in cards[2]["series"] if s["yaxis_id"] == "v"]
    assert len(names) == len(set(names))  # all 32 labels unique across packs


def test_chart_filters_reject_blank_states():
    # Number(null)/Number("") are 0 in JS — for the temp/power ranges 0 is valid,
    # so the filter must reject blank/null inputs explicitly (else spurious zeros).
    from custom_components.givenergy_local.dashboard import (
        _POWER_FILTER,
        _TEMP_FILTER,
        _VOLT_FILTER,
    )

    for flt in (_VOLT_FILTER, _TEMP_FILTER, _POWER_FILTER):
        assert "parseFloat" in flt and "isNaN" in flt


def test_no_battery_health_view_for_inverter_only():
    # No batteries (inverter-only install): skip the view rather than emit a
    # heatmap with batteries: [] (which the card's setConfig rejects).
    views = _views(bats=[])
    assert "Battery Health" not in views
    # the rest of the dashboard still generates fine
    assert "Overview" in views
    assert "Batteries" in views


def test_cell_voltages_list_removed_from_batteries_view():
    view = _views()["Batteries"]
    titles = [c.get("title") for s in view["sections"] for c in s["cards"]]
    assert "Cell Voltages" not in titles
    # the per-pack detail we keep is still there
    assert "Cell Temperatures" in titles
    assert "Pack Details" in titles
