from pathlib import Path

import polars as pl
import pytest

from rfsd_regional import dfo_filter, scan_rfsd
from rfsd_regional.analysis import (
    BASE, CLEAN, CLUSTER, GROSS_PROFIT, OKVED_CLASS, REVENUE, SECTION,
    SIMPLIFIED_RESULT, observed_sum, read_exclusions,
)
from rfsd_regional.sources import verify_file


def test_region_precedence_and_missing_values():
    frame = pl.DataFrame({
        "region_taxcode": ["7700", "2500", None, "", "broken", "0300"],
        "region": ["primorsky terr.", "moscow", "primorsky terr.", None, "buryatia", "buryatia"],
    })
    assert frame.select(dfo_filter()).to_series().to_list() == [False, True, True, False, False, True]


def test_sample_is_not_eligibility_or_a_hospital_blacklist():
    frame = pl.DataFrame({
        "okopf": ["12300", "65243", "75203", "20100", None, "12300", "12300"],
        "financial": [0, 0, 0, 0, 0, 1, 0],
        "okved_section": ["C", None, "Q", "A", "G", "K", "K"],
        "eligible": [0, 1, 0, 1, 1, 0, 1],
        "outlier": [0, None, 0, 0, 0, 0, 1],
    })
    assert frame.select(BASE).to_series().to_list() == [True, True, False, False, False, False, False]
    assert frame.filter(BASE & CLEAN).height == 1


def test_profit_forms_missing_costs_and_nonfinite_values():
    frame = pl.DataFrame({
        "simplified": [0, 0, 1, 1, 0, None],
        "line_2110": [100.0, 100.0, 100.0, 100.0, float("inf"), 100.0],
        "line_2120": [-60.0, None, -80.0, None, -10.0, -50.0],
        "line_2100": [35.0, None, None, None, float("nan"), None],
    })
    result = frame.select(GROSS_PROFIT.alias("full"), SIMPLIFIED_RESULT.alias("simple"))
    assert result["full"].to_list() == [35.0, None, None, None, None, None]
    assert result["simple"].to_list() == [None, None, 20.0, None, None, None]
    fallback = frame.head(1).with_columns(pl.lit(None, dtype=pl.Float64).alias("line_2100"))
    assert fallback.select(GROSS_PROFIT).item() == 40.0
    for values, expected in [([None, float("nan"), float("inf")], None), ([0.0, None], 0.0), ([-10.0, 20.0], 10.0)]:
        df = pl.DataFrame({"line_2110": values}, schema={"line_2110": pl.Float64})
        assert df.select(observed_sum(REVENUE)).item() == expected


def test_clusters_do_not_confuse_processing_with_fishing():
    df = pl.DataFrame({
        "okved": ["03.11", "10.20", "10.71", "11.01", "08.12", "20.11", "68.20", "16.10", "10.2", "46.90", None],
        "okved_section": ["A", "C", "C", "C", "B", "C", "L", "C", "C", "A", None],
    })
    result = df.select(CLUSTER.alias("cluster"), SECTION.alias("section"), OKVED_CLASS.alias("class"))
    assert result["cluster"].to_list() == [
        "Рыбный комплекс", "Рыбный комплекс", "Пищепром", "Прочее",
        "Недропользование и сырьё", "Недропользование и сырьё", "Торговля и потребсектор",
        "Прочее", "Рыбный комплекс", "Конфликт ОКВЭД", "ОКВЭД неизвестен",
    ]
    assert result["section"][5] == "C"
    assert result["class"].tail(3).to_list() == [None, None, None]


def test_scan_missing_year_and_installed_dictionary(tmp_path):
    partition = tmp_path / "year=2024"
    partition.mkdir()
    pl.DataFrame({"inn": ["0123456789"], "line_2110": [10.0]}).write_parquet(partition / "part-0.parquet")
    frame = scan_rfsd([2024, 2024], descriptive=True, data_dir=tmp_path).collect()
    assert frame["inn"].to_list() == ["0123456789"]
    assert frame["year"].to_list() == [2024]
    assert frame["PL_revenue"].item() == 10
    with pytest.raises(FileNotFoundError, match="2023"):
        scan_rfsd([2023, 2024], data_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="No RFSD"):
        scan_rfsd(data_dir=tmp_path / "empty")
    for invalid in [True, "2024", [2024, False], []]:
        with pytest.raises(TypeError):
            scan_rfsd(invalid, data_dir=tmp_path)


def test_manual_decisions_apply_only_to_the_specified_year(tmp_path):
    path = tmp_path / "decisions.csv"
    path.write_text("inn,year,action,reason,source\n0123456789,2024,review,,\n0123456789,2025,exclude,scale error,annual report\n", encoding="utf-8")
    assert read_exclusions(path).collect().rows() == [("0123456789", 2025)]
    path.write_text("inn,year,action,reason,source\n0123456789,2025,exclude,,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid decisions"):
        read_exclusions(path)


def test_checksum_rejects_modified_input(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"abc")
    sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    verify_file(path, sha)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="mismatch"):
        verify_file(path, sha)


def test_manual_exclusions_have_stable_order_and_no_duplicates(tmp_path):
    path = tmp_path / "decisions.csv"
    path.write_text("inn,year,action,reason,source\n"
                    "9999999999,2025,exclude,error,report\n"
                    "0123456789,2024,exclude,error,report\n"
                    "0123456789,2024,exclude,error,report\n", encoding="utf-8")
    assert read_exclusions(path).collect().rows() == [("0123456789", 2024), ("9999999999", 2025)]
