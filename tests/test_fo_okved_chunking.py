import pathlib
import openpyxl
import polars as pl
import pytest
from scripts.fo_okved_excel import DEFAULT_DISTRICT_PERIODS, partition_years_into_chunks, write_full_workbook


def test_chunking_predefined_districts():
    # SKFO
    skfo_counts = {y: 30000 for y in range(2011, 2026)}
    assert partition_years_into_chunks(skfo_counts, district_key="SKFO") == [(2011, 2025)]

    # Real counts check
    counts_data = {
        "SKFO": {2011: 30169, 2012: 31594, 2013: 33239, 2014: 34266, 2015: 35310, 2016: 36663, 2017: 37028, 2018: 35323, 2019: 33334, 2020: 31101, 2021: 28620, 2022: 26545, 2023: 25672, 2024: 26310, 2025: 27327},
        "YUFO": {2011: 98564, 2012: 103218, 2013: 107031, 2014: 114839, 2015: 122943, 2016: 128384, 2017: 126336, 2018: 121514, 2019: 116021, 2020: 98890, 2021: 90088, 2022: 84569, 2023: 80669, 2024: 77811, 2025: 76286},
        "SFO": {2011: 197942, 2012: 201524, 2013: 195106, 2014: 199134, 2015: 203608, 2016: 210792, 2017: 195006, 2018: 181219, 2019: 166939, 2020: 144402, 2021: 129982, 2022: 121517, 2023: 117675, 2024: 112613, 2025: 107500},
        "SZFO": {2011: 250632, 2012: 253937, 2013: 250793, 2014: 257093, 2015: 266195, 2016: 278207, 2017: 260437, 2018: 240888, 2019: 212090, 2020: 178763, 2021: 152280, 2022: 139165, 2023: 132011, 2024: 126226, 2025: 121727},
        "DFO": {2011: 65000, 2012: 66000, 2013: 67000, 2014: 68000, 2015: 70000, 2016: 72000, 2017: 71000, 2018: 69000, 2019: 67000, 2020: 64000, 2021: 61000, 2022: 60000, 2023: 59000, 2024: 58000, 2025: 57000},
    }

    for district, counts in counts_data.items():
        chunks = partition_years_into_chunks(counts, district_key=district)
        # Verify continuity: covered years must be exactly 2011..2025
        covered_years = []
        for s, e in chunks:
            assert s <= e
            assert e - s + 1 >= 1
            chunk_sum = sum(counts[y] for y in range(s, e + 1))
            assert chunk_sum < 1_000_000, f"{district} chunk {s}-{e} has {chunk_sum} >= 1,000,000"
            covered_years.extend(range(s, e + 1))
        assert covered_years == list(range(2011, 2026))


def test_chunking_dynamic_without_key():
    counts = {
        2011: 300000,
        2012: 300000,
        2013: 300000,
        2014: 300000,
        2015: 300000,
        2016: 300000,
    }
    # Total = 1.8M rows. Max 1M. min_years=2.
    # Should partition into 2 chunks of 3 years (900k each)
    chunks = partition_years_into_chunks(counts, max_rows=1_000_000, min_years=2)
    assert chunks == [(2011, 2013), (2014, 2016)]
    for s, e in chunks:
        assert sum(counts[y] for y in range(s, e + 1)) < 1_000_000


def test_chunking_with_missing_years():
    # Gap in years: 2012 is missing
    counts = {2011: 400_000, 2013: 400_000, 2014: 400_000, 2015: 400_000}
    # Should partition without KeyError
    chunks = partition_years_into_chunks(counts, max_rows=1_000_000, min_years=2)
    assert len(chunks) >= 2


def test_chunking_strict_max_rows_boundary():
    # A chunk with exactly 1,000,000 rows must be rejected when max_rows=1,000,000
    counts = {2011: 500_000, 2012: 500_000, 2013: 200_000, 2014: 200_000}
    # (2011, 2012) sum is 1,000,000, which is not < 1,000,000!
    with pytest.raises(ValueError):
        partition_years_into_chunks(counts, max_rows=1_000_000, min_years=2)


def test_chunking_empty_counts_raises():
    with pytest.raises(ValueError, match="empty"):
        partition_years_into_chunks({})


def test_chunking_exceeds_max_raises():
    counts = {2011: 1_200_000, 2012: 100_000}
    # A single year exceeds max_rows -> impossible
    with pytest.raises(ValueError):
        partition_years_into_chunks(counts, max_rows=1_000_000, min_years=1)


def test_write_full_workbook_structure_and_types(tmp_path):
    # Test workbook creation, sheet naming, cell types, and leading zero preservation
    df_data = pl.DataFrame({
        "year": [2024, 2024],
        "region_code": ["0500", "0700"],
        "region_name": ["Республика Дагестан", "Кабардино-Балкарская Республика"],
        "inn": ["0501001234", "0725012540"],  # Leading zeros!
        "ogrn": ["0123456789012", "1130725001978"],  # Leading zero!
        "okved": ["10.11", "47.11"],
        "okved_division": [10, 47],
        "section": ["C", "G"],
        "okopf": ["12300", "12300"],
        "line_2110_PL_revenue": [1000.0, 2000.0],
        "outlier": [0.0, 0.0],
    })

    df_summary = pl.DataFrame({
        "year": [2024],
        "region_name": ["Республика Дагестан"],
        "okved_division": [10],
        "section": ["C"],
        "firms_total": [1],
        "firms_filed": [1],
        "firms_revenue_positive": [1],
        "revenue_bln": [1.0],
        "gross_profit_bln": [0.5],
        "net_profit_bln": [0.2],
        "assets_bln": [2.0],
        "equity_bln": [1.5],
    })

    xlsx_path = tmp_path / "test_wb.xlsx"
    write_full_workbook(xlsx_path, df_data=df_data, df_summary=df_summary)

    # Inspect with openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    assert "Сводная" in wb.sheetnames
    assert "Данные" in wb.sheetnames

    ws_data = wb["Данные"]
    # Row 1 is header, Row 2 is first data row
    row2 = [cell.value for cell in ws_data[2]]
    # Check INN and OGRN are string types with leading zeros intact
    inn_val = row2[3]
    ogrn_val = row2[4]
    assert inn_val == "0501001234"
    assert isinstance(inn_val, str)
    assert ogrn_val == "0123456789012"
    assert isinstance(ogrn_val, str)

    wb.close()
