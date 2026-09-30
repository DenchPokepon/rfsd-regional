"""
DFO Regional Analysis Script.
Implements the tasks from Sergey Anufrievich & Denis Krylov:
1. Activity in Far Eastern Federal District (ДФО) by OKVED (detail: xx.xx).
   Activity = number of firms, total revenue for a representative period, by region.
2. Sampling top active OKVED codes.
3. Identifying the head ("голова") of companies in each top sector.
"""
from pathlib import Path
import sys
import argparse

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import polars as pl
from firm_level import scan_rfsd, dfo_filter, load_renaming_dict

OUTPUTS_DIR = Path(__file__).resolve().parent.parent / "outputs"


def analyze_dfo(
    year: int = 2024,
    top_k_sectors: int = 15,
    top_k_firms: int = 10,
    save_outputs: bool = True,
):
    print(f"=== Running DFO Analysis for Year {year} ===")
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. Native Polars LazyFrame with DFO filter
    lf = (
        scan_rfsd(year=year)
        .filter(dfo_filter())
        .with_columns([
            # Standardize okved to 4 digits / xx.xx level (first 5 characters, e.g. '03.11', '46.90')
            pl.col("okved").str.slice(0, 5).alias("okved_group"),
            # Ensure revenue is clean
            pl.col("line_2110").fill_null(0.0).alias("revenue_clean"),
            pl.col("line_1600").fill_null(0.0).alias("assets_clean"),
            pl.col("line_2400").fill_null(0.0).alias("net_profit_clean"),
        ])
    )

    # 2. Activity by OKVED (xx.xx) across entire DFO
    print("Computing DFO activity by OKVED (xx.xx)...")
    okved_summary = (
        lf.filter(pl.col("okved_group").is_not_null() & (pl.col("okved_group") != ""))
        .group_by("okved_group")
        .agg([
            pl.len().alias("firm_count"),
            pl.col("revenue_clean").sum().alias("total_revenue_rub_thous"),
            pl.col("revenue_clean").mean().alias("mean_revenue_rub_thous"),
            pl.col("assets_clean").sum().alias("total_assets_rub_thous"),
            pl.col("net_profit_clean").sum().alias("total_profit_rub_thous"),
            pl.col("region").n_unique().alias("regions_present"),
        ])
        .with_columns([
            (pl.col("total_revenue_rub_thous") / 1e6).round(2).alias("total_revenue_bln_rub"),
            (pl.col("total_assets_rub_thous") / 1e6).round(2).alias("total_assets_bln_rub"),
        ])
        .sort("total_revenue_rub_thous", descending=True)
        .collect()
    )

    print("\n--- TOP OKVEDs BY REVENUE IN DFO ---")
    print(
        okved_summary.select([
            "okved_group",
            "firm_count",
            "total_revenue_bln_rub",
            "total_assets_bln_rub",
            "regions_present",
        ]).head(top_k_sectors)
    )

    # 3. Regional breakdown: Activity by (region, okved_group)
    print("\nComputing Regional Activity by (region, okved)...")
    regional_summary = (
        lf.filter(pl.col("okved_group").is_not_null() & (pl.col("okved_group") != ""))
        .group_by(["region", "okved_group"])
        .agg([
            pl.len().alias("firm_count"),
            (pl.col("revenue_clean").sum() / 1e6).round(2).alias("total_revenue_bln_rub"),
            (pl.col("assets_clean").sum() / 1e6).round(2).alias("total_assets_bln_rub"),
        ])
        .sort(["region", "total_revenue_bln_rub"], descending=[False, True])
        .collect()
    )

    # 4. Identify the "head" (крупнейшие действующие компании) for top sectors
    top_sector_codes = okved_summary["okved_group"].head(top_k_sectors).to_list()
    
    print(f"\nIdentifying 'Head' companies for top {len(top_sector_codes)} sectors...")
    head_firms = (
        lf.filter(
            pl.col("okved_group").is_in(top_sector_codes)
            & (pl.col("revenue_clean") > 0)
        )
        .select([
            "year",
            "inn",
            "ogrn",
            "region",
            "region_taxcode",
            "okved",
            "okved_group",
            (pl.col("revenue_clean") / 1e6).round(3).alias("revenue_bln_rub"),
            (pl.col("assets_clean") / 1e6).round(3).alias("assets_bln_rub"),
            (pl.col("net_profit_clean") / 1e6).round(3).alias("net_profit_bln_rub"),
        ])
        .sort(["okved_group", "revenue_bln_rub"], descending=[False, True])
        .collect()
        .group_by("okved_group")
        .head(top_k_firms)
        .sort(["okved_group", "revenue_bln_rub"], descending=[False, True])
    )

    print("\n--- SAMPLE 'HEAD' FIRMS IN DFO ---")
    print(head_firms.head(25))

    if save_outputs:
        okved_summary_path = OUTPUTS_DIR / f"dfo_okved_activity_{year}.csv"
        regional_summary_path = OUTPUTS_DIR / f"dfo_regional_okved_activity_{year}.csv"
        head_firms_path = OUTPUTS_DIR / f"dfo_head_firms_{year}.csv"

        # Write CSV with quote_style="always" to preserve leading zeroes in INN, OGRN, OKVED, and taxcodes
        okved_summary.write_csv(okved_summary_path, quote_style="always")
        regional_summary.write_csv(regional_summary_path, quote_style="always")
        head_firms.write_csv(head_firms_path, quote_style="always")

        # Also save companion parquet files for 100% type fidelity
        okved_summary.write_parquet(OUTPUTS_DIR / f"dfo_okved_activity_{year}.parquet")
        regional_summary.write_parquet(OUTPUTS_DIR / f"dfo_regional_okved_activity_{year}.parquet")
        head_firms.write_parquet(OUTPUTS_DIR / f"dfo_head_firms_{year}.parquet")

        print(f"\nOutputs successfully saved to:")
        print(f"  - {okved_summary_path} (.csv & .parquet)")
        print(f"  - {regional_summary_path} (.csv & .parquet)")
        print(f"  - {head_firms_path} (.csv & .parquet)")

    return {
        "okved_summary": okved_summary,
        "regional_summary": regional_summary,
        "head_firms": head_firms,
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze DFO enterprise activity by OKVED")
    parser.add_argument("--year", type=int, default=2024, help="Year to analyze (default: 2024)")
    parser.add_argument("--top-sectors", type=int, default=15, help="Number of top sectors")
    parser.add_argument("--top-firms", type=int, default=10, help="Number of top firms per sector")
    args = parser.parse_args()

    analyze_dfo(year=args.year, top_k_sectors=args.top_sectors, top_k_firms=args.top_firms)


if __name__ == "__main__":
    main()
