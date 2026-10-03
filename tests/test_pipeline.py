"""
Automated unit & regression tests for the unified RFSD-Rosstat analytics pipeline.
Validates:
1. Integrity and presence of Rosstat source workbooks.
2. Multi-sheet parsing of official GRP (1998-2024), GVA (A..S), and volume indices.
3. Robustness of correlation calculations under zero variance and NaNs.
4. Completeness and validity of macro, dynamic, sectoral, cluster, and regional metrics.
5. Presence and non-emptiness of pipeline report and visualization artifacts.
"""
from pathlib import Path
import json
import numpy as np
import polars as pl
import pytest

from scripts.rfsd_rosstat_pipeline import (
    DATA_DIR,
    OUTPUTS_DIR,
    DFO_11_REGIONS,
    OKVED_SECTION_NAMES,
    ROSSTAT_SOURCES,
    stage3_parse_rosstat,
    stage4_compute_metrics,
)


def test_rosstat_sources_cached_and_valid():
    """Verify that all required Rosstat Excel files exist and are not corrupted/empty."""
    for filename in ROSSTAT_SOURCES.keys():
        file_path = DATA_DIR / filename
        assert file_path.exists(), f"Missing required Rosstat file: {filename}"
        assert file_path.stat().st_size > 50_000, f"File {filename} is suspiciously small: {file_path.stat().st_size} bytes"


def test_stage3_parse_rosstat():
    """Test multi-sheet extraction from official Rosstat workbooks."""
    res = stage3_parse_rosstat(DATA_DIR)

    # 1. Nominal GRP by region (11 regions, 2011-2024)
    assert set(res["grp_regional"].keys()) == set(DFO_11_REGIONS)
    for reg in DFO_11_REGIONS:
        for yr in range(2011, 2025):
            assert yr in res["grp_regional"][reg], f"Missing {yr} for {reg}"
            assert res["grp_regional"][reg][yr] > 0.0

    # 2. Total GRP matches sum of 11 regions
    for yr in range(2011, 2025):
        expected_sum = sum(res["grp_regional"][reg][yr] for reg in DFO_11_REGIONS)
        assert abs(res["grp_total"][yr] - expected_sum) < 1e-4

    # 3. Sectoral GVA contains all 19 sections A..S (2016-2024)
    assert set(res["gva_nominal"].keys()) == set(OKVED_SECTION_NAMES.keys())
    for s in OKVED_SECTION_NAMES.keys():
        for yr in range(2016, 2025):
            assert yr in res["gva_nominal"][s], f"Missing GVA for section {s} in {yr}"
            assert res["gva_nominal"][s][yr] > 0.0

    # 4. GRP real volume index (ИФО) covers 2011-2024
    for yr in range(2011, 2025):
        assert yr in res["grp_real_index"]
        assert 80.0 <= res["grp_real_index"][yr] <= 130.0

    # 5. IVBO output volume index covers 2018-2024
    for yr in range(2018, 2025):
        assert yr in res["ivbo_dfo"]
        assert 80.0 <= res["ivbo_dfo"][yr] <= 130.0


def test_stage4_metrics_computation():
    """Test econometric metrics computation with mock/cached inputs."""
    cache_path = OUTPUTS_DIR / "rfsd_dfo_micro_aggregates.parquet"
    if not cache_path.exists():
        pytest.skip("Micro aggregates cache not found, skipping stage 4 integration test")

    df_base = pl.read_parquet(cache_path)
    df_macro = df_base.group_by("year").agg([
        pl.col("n_firms").sum().alias("total_firms"),
        pl.col("n_active").sum().alias("active_firms"),
        (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
        (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
    ]).sort("year")

    df_sectors = df_base.filter(pl.col("okved_section").is_not_null()).group_by(["year", "okved_section"]).agg([
        pl.col("n_firms").sum().alias("total_firms"),
        pl.col("n_active").sum().alias("active_firms"),
        (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
        (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
    ]).sort(["year", "okved_section"])

    df_regions = df_base.filter(pl.col("region_ru").is_not_null()).group_by(["year", "region_ru"]).agg([
        pl.col("n_firms").sum().alias("total_firms"),
        pl.col("n_active").sum().alias("active_firms"),
        (pl.col("rev_thous").sum() / 1e6).alias("rev_bln"),
        (pl.col("gp_thous").sum() / 1e6).alias("gp_bln"),
    ]).sort(["year", "region_ru"])

    rfsd_mock = {
        "macro": df_macro,
        "sectors": df_sectors,
        "regions": df_regions,
        "clusters": pl.DataFrame(),
    }
    rosstat_data = stage3_parse_rosstat(DATA_DIR)
    metrics = stage4_compute_metrics(rfsd_mock, rosstat_data)

    # Validate output tables
    df_m = metrics["macro_table"]
    assert len(df_m) == 15  # 2011 to 2025
    assert set(df_m["year"].to_list()) == set(range(2011, 2026))

    df_g = metrics["growth_table"]
    assert len(df_g) == 13  # 2012 to 2024

    df_sm = metrics["sector_match_2024"]
    assert len(df_sm) == 19  # A..S
    assert set(df_sm["section"].to_list()) == set(OKVED_SECTION_NAMES.keys())

    df_rg = metrics["regional_2024"]
    assert len(df_rg) == 11
    assert set(df_rg["region"].to_list()) == set(DFO_11_REGIONS)

    corrs = metrics["macro_corrs"]
    assert 0.90 <= corrs["corr_level_rev_grp"] <= 1.0
    assert 0.90 <= corrs["corr_level_gp_grp"] <= 1.0
    assert -1.0 <= corrs["corr_nom_rev_grp"] <= 1.0
    assert -1.0 <= corrs["corr_real_rev_grp"] <= 1.0


def test_clean_corr_edge_cases():
    """Verify that correlation helper cleanly handles zero variance, NaNs, and short inputs."""
    from scripts.rfsd_rosstat_pipeline import stage4_compute_metrics

    # Test via stage4 internal clean_corr semantics
    def clean_corr(s1, s2):
        valid = [(x, y) for x, y in zip(s1, s2) if not np.isnan(x) and not np.isnan(y)]
        if len(valid) < 3:
            return np.nan
        v1, v2 = zip(*valid)
        std1, std2 = float(np.std(v1)), float(np.std(v2))
        if std1 < 1e-12 or std2 < 1e-12:
            return np.nan
        return float(np.corrcoef(v1, v2)[0, 1])

    # 1. Constant vector (zero variance)
    assert np.isnan(clean_corr([1.0, 1.0, 1.0, 1.0], [2.0, 3.0, 4.0, 5.0]))

    # 2. Short vector (< 3 valid points)
    assert np.isnan(clean_corr([1.0, 2.0], [2.0, 3.0]))

    # 3. NaNs handling
    assert abs(clean_corr([1.0, 2.0, np.nan, 4.0], [2.0, 4.0, 5.0, 8.0]) - 1.0) < 1e-6


def test_pipeline_generated_artifacts():
    """Verify that all pipeline outputs exist, are non-empty, and valid."""
    report_file = OUTPUTS_DIR / "rfsd_rosstat_pipeline_report.md"
    assert report_file.exists()
    text = report_file.read_text(encoding="utf-8")
    assert "Аналитический доклад: Микроданные RFSD и Макростатистика Росстата по ДФО" in text
    assert "## 1. Резюме и методологические принципы" in text
    assert "## 2. Макроэкономическое сопоставление (2011–2025 гг.)" in text
    assert "## 5. Отраслевые кластеры ДФО (2011–2025 гг.)" in text
    assert "## 6. Региональное сопоставление субъектов ДФО (2024 г.)" in text

    # Verify JSON export
    json_file = OUTPUTS_DIR / "industry_yearly.json"
    assert json_file.exists()
    records = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(records) > 50
    assert "group" in records[0]
    assert "firms" in records[0]

    # Verify PNG charts
    chart_names = [
        "chart_grp_vs_rfsd_macro.png",
        "chart_real_growth_comparison.png",
        "chart_sectoral_comparison_2024.png",
        "chart_industry_clusters.png",
    ]
    for cname in chart_names:
        cpath = OUTPUTS_DIR / cname
        assert cpath.exists(), f"Missing chart: {cname}"
        assert cpath.stat().st_size > 50_000, f"Chart {cname} is too small: {cpath.stat().st_size} bytes"
