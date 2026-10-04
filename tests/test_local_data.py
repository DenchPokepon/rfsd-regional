"""Optional integration checks for the pinned data, not tests of generated files."""

import polars as pl
import pytest

from rfsd_regional import dfo_filter, scan_rfsd
from rfsd_regional.analysis import BASE, CLEAN, REVENUE, observed_sum
from rfsd_regional.dataset import DATA_DIR, RFSD_DIR
from rfsd_regional.rosstat import read_grp, read_gva, read_comparison_details

pytestmark = pytest.mark.integration


def test_pinned_rfsd_sample_and_hospital():
    if not (RFSD_DIR / "year=2024").exists():
        pytest.skip("Download RFSD to run the local integration checks")
    lf = scan_rfsd(year=2024)
    total, unique = lf.select(pl.len(), pl.col("inn").n_unique()).collect().row(0)
    assert total == unique
    base = lf.filter(dfo_filter() & BASE)
    for sample, count, revenue in [(base, 134934, 12247.178070), (base.filter(CLEAN), 134933, 12224.275389)]:
        n, rev = sample.select(pl.len(), observed_sum(REVENUE) / 1e6).collect().row(0)
        assert n == count
        assert rev == pytest.approx(revenue, abs=1e-6)
    row = lf.filter(pl.col("inn") == "2526003992").select("outlier", "imputed", BASE.alias("base")).collect()
    assert row.rows() == [(0.0, 1.0, False)]


def test_pinned_rosstat_totals():
    if not (DATA_DIR / "VRP_OKVED2_s_2016.xlsx").exists():
        pytest.skip("Download Rosstat files to run the local integration checks")
    grp = read_grp(DATA_DIR / "VRP_s1998.xlsx")
    gva = read_gva(DATA_DIR / "VRP_OKVED2_s_2016.xlsx")
    assert grp.filter(pl.col("year") == 2011)["grp_bln"].sum() == pytest.approx(2890.0653)
    assert grp.filter(pl.col("year") == 2024)["grp_bln"].sum() == pytest.approx(11769.9991)
    assert gva.filter(pl.col("year") == 2024)["gva_bln"].sum() == pytest.approx(11769.9991, abs=0.001)


def test_pinned_rosstat_indices_and_housing():
    if not (DATA_DIR / "VRP_OKVED2_s_2016.xlsx").exists():
        pytest.skip("Download Rosstat files to run the local integration checks")
    details = read_comparison_details(DATA_DIR / "VRP_s1998.xlsx", DATA_DIR / "VRP_OKVED2_s_2016.xlsx")
    volumes = details["volume_indices"]
    assert volumes.height == 248
    assert volumes.filter((pl.col("group") == "DFO") & (pl.col("year") == 2024))["volume_factor"].item() == 1.042
    # Preserve the anomalous source zero; growth calculations must not use it as a deflator.
    assert volumes.filter((pl.col("group") == "S") & (pl.col("year") == 2019))["volume_factor"].item() == 0
    housing = details["housing"].filter((pl.col("region_code") == "DFO") & (pl.col("year") == 2024))
    assert housing["housing_bln"].item() == pytest.approx(513.5449899)
    gva = details["regional_gva"].filter(pl.col("year") == 2024)
    total = gva.filter(pl.col("region_code") == "DFO")["gva_bln"].sum()
    assert total == pytest.approx(gva.filter(pl.col("region_code") != "DFO")["gva_bln"].sum(), abs=.001)
