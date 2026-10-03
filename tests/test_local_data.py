"""Optional integration checks for the pinned data, not tests of generated files."""

import polars as pl
import pytest

from firm_level import dfo_filter, scan_rfsd
from firm_level.analysis import BASE, CLEAN, REVENUE, observed_sum
from firm_level.dataset import DATA_DIR, RFSD_DIR
from firm_level.rosstat import read_grp, read_gva

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
