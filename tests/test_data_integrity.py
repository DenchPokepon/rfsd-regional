"""
Characterization test suite for RFSD data integrity and Polars query engine.
Validates:
1. All 15 annual partitions (2011-2025) are present on disk.
2. Schema integrity (columns, types).
3. Primary key uniqueness (zero duplicate INNs per year partition).
4. Descriptive renaming dictionary integrity (no duplicate targets).
5. Strict type enforcement on year parameter (no implicit casting).
6. Exact reproduction of Bank of Russia benchmark (Mogilyat et al., 2024).
"""
import pytest
import polars as pl
from firm_level import (
    scan_rfsd,
    load_renaming_dict,
    dfo_filter,
    DFO_REGIONS,
    DFO_TAXCODES,
    RFSD_DIR,
    NAMES_DICT_PATH,
)


def test_partitions_present():
    """Verify that all 15 annual partitions (2011-2025) exist on local disk."""
    expected_years = list(range(2011, 2026))
    partitions = sorted(RFSD_DIR.glob("year=*/part-0.parquet"))
    found_years = [int(p.parent.name.split("=")[1]) for p in partitions]
    
    assert found_years == expected_years, f"Missing years: {set(expected_years) - set(found_years)}"
    for p in partitions:
        assert p.stat().st_size > 200 * 1024 * 1024, f"File {p} is suspiciously small: {p.stat().st_size} bytes"


def test_schema_integrity():
    """Verify schema has 224 columns and core variables with expected types."""
    lf = scan_rfsd(year=2024)
    schema = lf.collect_schema()
    
    assert len(schema) == 224, f"Expected 224 columns, got {len(schema)}"
    assert schema.get("inn") == pl.String
    assert schema.get("ogrn") == pl.String
    assert schema.get("year") == pl.Int64
    assert schema.get("region") == pl.String
    assert schema.get("okved") == pl.String
    assert schema.get("line_2110") == pl.Float64  # Revenue
    assert schema.get("line_1600") == pl.Float64  # Total assets
    assert schema.get("line_2400") == pl.Float64  # Net profit


def test_descriptive_names_no_duplicates():
    """Ensure descriptive renaming dictionary has zero duplicate target column names."""
    renames = load_renaming_dict()
    assert len(renames) > 0, "Renaming dictionary is empty"
    
    targets = list(renames.values())
    unique_targets = set(targets)
    assert len(targets) == len(unique_targets), (
        f"Found duplicate target column names in {NAMES_DICT_PATH}: "
        f"{[t for t in unique_targets if targets.count(t) > 1]}"
    )

    # Test that scan_rfsd(descriptive=True) executes cleanly without DuplicateError
    lf_desc = scan_rfsd(year=2024, descriptive=True)
    schema_desc = lf_desc.collect_schema()
    assert "PL_revenue" in schema_desc
    assert "B_total_assets" in schema_desc
    assert "B_borrowings_longterm" in schema_desc
    assert "B_borrowings_shortterm" in schema_desc


def test_strict_year_type_validation_no_implicit_casting():
    """Verify that scan_rfsd strictly enforces int types without implicit casting."""
    # Valid int
    lf_int = scan_rfsd(year=2024)
    assert lf_int is not None

    # Valid sequence of ints
    lf_seq = scan_rfsd(year=[2023, 2024])
    assert lf_seq is not None

    # String year must be rejected
    with pytest.raises(TypeError, match="no implicit casting allowed"):
        scan_rfsd(year="2024")

    # Sequence containing strings must be rejected
    with pytest.raises(TypeError, match="without implicit casting"):
        scan_rfsd(year=["2023", "2024"])

    # Boolean must be rejected
    with pytest.raises(TypeError, match="no implicit casting allowed"):
        scan_rfsd(year=True)


def test_dfo_filter():
    """Verify DFO filter predicate matches all 11 Far East regions."""
    df_dfo = (
        scan_rfsd(year=2024)
        .filter(dfo_filter())
        .select(["region", "region_taxcode"])
        .unique()
        .collect()
    )
    
    found_regions = set(df_dfo["region"].to_list())
    assert found_regions.issubset(set(DFO_REGIONS))
    assert len(found_regions) == 11, f"Expected 11 DFO regions, found {len(found_regions)}: {found_regions}"


def test_primary_key_uniqueness():
    """Verify that INN is strictly unique within an annual partition (zero duplicates)."""
    lf = scan_rfsd(year=2024)
    counts = lf.select([
        pl.len().alias("total_rows"),
        pl.col("inn").n_unique().alias("unique_inns"),
    ]).collect()
    
    total = counts["total_rows"][0]
    unique = counts["unique_inns"][0]
    assert total == unique, f"Duplicate INNs found in year 2024: total={total}, unique={unique}"
    assert total > 3_000_000, f"Unexpectedly low firm count in 2024: {total}"


def test_cbr_cost_channel_benchmark():
    """
    Characterization test reproducing official Bank of Russia benchmark (Mogilyat et al., 2024).
    Verifies that the exact row counts match published RFSD numbers for 2019-2023.
    """
    lf_cbr = scan_rfsd(year=[2019, 2020, 2021, 2022, 2023])

    # Step 1: Non-financial firms (okved_section != 'K') -> 5,003,225
    s1 = lf_cbr.filter(pl.col("okved_section") != "K")
    n1 = s1.select(pl.col("inn").n_unique()).collect().item()
    assert n1 == 5_003_225, f"Step 1 mismatch: expected 5,003,225, got {n1}"

    # Step 2: Filed statements or imputed -> 2,996,612
    s2 = s1.filter((pl.col("filed") == 1) | (pl.col("imputed") == 1))
    n2 = s2.select(pl.col("inn").n_unique()).collect().item()
    assert n2 == 2_996_612, f"Step 2 mismatch: expected 2,996,612, got {n2}"
