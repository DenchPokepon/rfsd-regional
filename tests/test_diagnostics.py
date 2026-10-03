import polars as pl
import pytest

from firm_level.diagnostics import BUCKET, adjacent_panel, changes


def panel(year, revenues, regions, sections):
    return pl.DataFrame({
        "inn": ["0000000001", "0000000002", "0000000003", "0000000004"],
        "year": [year] * 4, "revenue": revenues, "region_code": regions,
        "section": sections, "imputed": [0, 0, 1, 0],
    }, schema_overrides={"revenue": pl.Float64})


def test_adjacent_panel_keeps_zero_but_not_missing_or_moving_firms():
    before = panel(2023, [100., 0., 50., 30.], ["2500"] * 4, ["C"] * 4)
    after = panel(2024, [120., 10., None, 40.], ["2500"] * 3 + ["2700"], ["C"] * 4)
    row = adjacent_panel(before, after, "total").row(0, named=True)
    assert row["panel_firms"] == 2
    assert row["panel_previous_bln"] == pytest.approx(100 / 1e6)
    assert row["panel_current_bln"] == pytest.approx(130 / 1e6)
    assert row["panel_growth"] == pytest.approx(.3)
    assert row["aggregate_growth"] == pytest.approx(170 / 180 - 1)
    aggregate_change = row["aggregate_current_bln"] - row["aggregate_previous_bln"]
    panel_change = row["panel_current_bln"] - row["panel_previous_bln"]
    outside_change = row["outside_panel_current_bln"] - row["outside_panel_previous_bln"]
    assert aggregate_change == pytest.approx(panel_change + outside_change)


def test_sector_panel_excludes_switches_unknowns_and_nonadjacent_years():
    before = panel(2023, [100., 0., 50., 30.], ["2500"] * 4, ["C", "C", "unknown", "C"])
    after = panel(2024, [120., 10., 60., 40.], ["2500"] * 4, ["G", "C", "unknown", "C"])
    result = adjacent_panel(before, after, "section")
    row = result.filter(pl.col("group") == "C").row(0, named=True)
    assert row["panel_firms"] == 2
    assert row["panel_growth"] == pytest.approx(50 / 30 - 1)
    assert result.filter(pl.col("group") == "unknown")["panel_growth"].item() is None
    nonadjacent = adjacent_panel(before, after.with_columns(pl.lit(2025).alias("year")), "total")
    assert nonadjacent["panel_growth"].item() is None
    zero = adjacent_panel(before.with_columns(pl.lit(0.).alias("revenue")), after, "total")
    assert zero["panel_growth"].item() is None


def test_exclusion_buckets_are_disjoint_and_respect_missing_flags():
    frame = pl.DataFrame({
        "okopf": ["75203", None, "12300", "12300", "12300", "12300", "12300", "12300"],
        "financial": [0, 0, 1, None, 0, 0, 0, 0], "okved_section": ["C"] * 8,
        "outlier": [1, 1, 1, 0, 1, None, 0, 0],
        "manual_exclusion": [True] * 7 + [False],
    })
    assert frame.select(BUCKET).to_series().to_list() == [
        "noncommercial", "other_or_unknown", "financial", "financial_unknown",
        "outlier", "outlier_unknown", "manual_exclusion", "clean",
    ]


def test_changes_do_not_turn_disappearance_into_zero_or_exit():
    before = panel(2023, [100., 0., None, 30.], ["2500"] * 4, ["C"] * 4).with_columns(
        pl.lit("10.10").alias("okved"), pl.lit("food").alias("cluster"),
        pl.lit(True).alias("base"), pl.lit(True).alias("clean"),
        pl.lit(1.).alias("filed"), pl.lit(0.).alias("outlier"), pl.lit(0.).alias("simplified"),
    )
    after = before.filter(pl.col("inn") != "0000000001").with_columns(pl.lit(2024).alias("year"))
    result = changes(before, after)
    missing = result.filter(pl.col("inn") == "0000000001").row(0, named=True)
    assert missing["revenue"] is None and missing["revenue_previous"] == 100
    assert missing["year"] is None and missing["year_previous"] == 2023
    assert missing["revenue_change"] is None
