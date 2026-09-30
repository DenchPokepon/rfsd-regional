"""
verify_reproduction.py
Reproduces the exact benchmark results from the official RFSD repository:
1. Macro Universe Totals (2011-2025): Total rows and unique INNs.
2. Official Bank of Russia Cost-Channel Replication (Mogilyat et al., 2024 / Skougarevskiy, 2025):
   Verifies every sequential filtering step on 2019-2023 data against the published benchmarks.
"""
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import time
import polars as pl
from firm_level import scan_rfsd

def main():
    print("=" * 70)
    print("  RFSD Dataset Verification & Benchmark Reproduction")
    print("=" * 70)
    
    t0 = time.time()
    
    # -------------------------------------------------------------
    # 1. Total Universe Verification
    # -------------------------------------------------------------
    print("\n[Part 1] Computing 2011-2025 Universe Totals...")
    lf_all = scan_rfsd()
    total_obs = lf_all.select(pl.len()).collect().item()
    unique_inns = lf_all.select(pl.col("inn").n_unique()).collect().item()
    
    print(f"  * Total firm-year observations: {total_obs:,}")
    print(f"  * Total unique legal entities (INN): {unique_inns:,}")

    # -------------------------------------------------------------
    # 2. Reproduction of Official CBR Cost Channel Study
    #    (Mogilyat et al., 2024 replicated by RFSD authors)
    # -------------------------------------------------------------
    print("\n[Part 2] Reproducing Official Bank of Russia Study Benchmark (2019-2023)...")
    print("Reference: Mogilyat et al. (2024) / RFSD use_cases/interest_payments.Rmd\n")

    cols = [
        "inn", "year", "okved_section", "filed", "imputed",
        "line_1600", "line_1700", "line_2110", "line_2120",
        "line_2200", "line_2300", "line_2330", "line_1410", "line_1510",
    ]
    
    # Base 2019-2023 LazyFrame
    lf_cbr = scan_rfsd(year=[2019, 2020, 2021, 2022, 2023]).select(cols)

    benchmarks = [
        ("Step 1: Non-financial firms (okved_section != 'K')", 5_003_225,
         lambda lf: lf.filter(pl.col("okved_section") != "K")),
        
        ("Step 2: Filed statements or imputed (filed==1 | imputed==1)", 2_996_612,
         lambda lf: lf.filter((pl.col("filed") == 1) | (pl.col("imputed") == 1))),
        
        ("Step 3: Non-missing core financial items", 2_255_328,
         lambda lf: lf.filter(
             pl.col("line_2110").is_not_null() &
             pl.col("line_2120").is_not_null() &
             pl.col("line_2200").is_not_null() &
             pl.col("line_2300").is_not_null()
         )),
        
        ("Step 4: Non-zero revenue & cost of goods sold", 2_174_904,
         lambda lf: lf.filter((pl.col("line_2110") != 0) & (pl.col("line_2120") != 0))),
        
        ("Step 5: Articulated balance sheet (assets == liabilities)", 2_172_787,
         lambda lf: lf.filter(pl.col("line_1600") == pl.col("line_1700"))),
        
        ("Step 6: Active interest payers (line_2330 != 0)", 372_456,
         lambda lf: lf.filter(pl.col("line_2330").is_not_null() & (pl.col("line_2330") != 0))),
    ]

    curr_lf = lf_cbr
    for desc, expected, filter_fn in benchmarks:
        curr_lf = filter_fn(curr_lf)
        actual = curr_lf.select(pl.col("inn").n_unique()).collect().item()
        match_str = "MATCH [OK]" if actual == expected else f"DIFF: {actual - expected:+d}"
        print(f"  {desc:<58} Actual: {actual:>9,d} | Expected: {expected:>9,d} | {match_str}")

    # Step 7: Trim 1.5% and 98.5% quantiles of interest_cost_ratio
    print("\n[Part 3] Step 7: Outlier trimming & Figure 2 / Figure 3 replication:")
    df_prepared = (
        curr_lf.with_columns([
            (pl.col("line_2330").abs() / pl.col("line_2120").abs()).alias("interest_cost_ratio"),
            pl.when((pl.col("line_1410").fill_null(0).abs() > 0) | (pl.col("line_1510").fill_null(0).abs() > 0))
              .then(1).otherwise(0).alias("has_debt")
        ])
        .collect()
    )
    
    q015 = df_prepared["interest_cost_ratio"].quantile(0.015)
    q985 = df_prepared["interest_cost_ratio"].quantile(0.985)
    trimmed_df = df_prepared.filter(
        (pl.col("interest_cost_ratio") >= q015) & (pl.col("interest_cost_ratio") <= q985)
    )
    
    trimmed_unique = trimmed_df["inn"].n_unique()
    print(f"  Final sample unique firms (trimmed): {trimmed_unique:,} (Expected: 365,002 | diff: {trimmed_unique - 365002:+d})")

    # Yearly breakdown
    by_year = (
        trimmed_df.group_by("year")
        .agg([
            pl.len().alias("all_firms"),
            pl.col("has_debt").sum().alias("firms_with_debt"),
            (pl.col("interest_cost_ratio").median() * 100).round(2).alias("median_interest_ratio_%"),
        ])
        .sort("year")
    )
    print("\nReplicated Yearly Summary (Figure 2 & Figure 3):")
    print(by_year)
    
    print(f"\nVerification successfully completed in {time.time() - t0:.1f}s.")
    print("=" * 70)

if __name__ == "__main__":
    main()
