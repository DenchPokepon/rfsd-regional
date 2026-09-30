"""
explore.py - Quickstart & Interactive Entry Point for RFSD Analysis.

Usage:
    uv run python explore.py          # Run demo queries & print summary
    uv run python -i explore.py       # Open interactive Python shell with `lf` pre-loaded
"""
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import polars as pl
from firm_level import (
    scan_rfsd,
    dfo_filter,
    load_renaming_dict,
    DFO_REGIONS,
    DFO_TAXCODES,
    RFSD_DIR,
)

# Set high-readability printing defaults for Polars
pl.Config.set_tbl_rows(25)
pl.Config.set_tbl_cols(12)
pl.Config.set_tbl_width_chars(120)

# Pre-initialized LazyFrames
lf = scan_rfsd()
lf_desc = scan_rfsd(descriptive=True)
dfo = dfo_filter()


def demo():
    print("=" * 60)
    print("  RFSD Firm-Level Environment (Polars Lazy Engine)")
    print("=" * 60)
    
    # 1. Available partitions
    partitions = sorted(RFSD_DIR.glob("year=*/part-0.parquet"))
    years = [int(p.parent.name.split("=")[1]) for p in partitions]
    print(f"Dataset location: {RFSD_DIR}")
    print(f"Downloaded years ({len(years)}): {years}\n")

    if not partitions:
        print("No partitions found yet. Run `uv run python scripts/download_data.py`")
        return

    # 2. Pick latest available year for demo
    latest_year = max(years)
    print(f"--- Running Demo on Year {latest_year} ---")

    # Native Polars query: Top 10 Russian firms by Revenue (line_2110)
    print(f"\n[1] Top 10 Russian firms by Revenue (year {latest_year}):")
    top_firms = (
        lf.filter((pl.col("year") == latest_year) & pl.col("line_2110").is_not_null())
        .select([
            "year",
            "inn",
            "region",
            "okved",
            (pl.col("line_2110") / 1e6).round(2).alias("revenue_bln_rub"),
            (pl.col("line_2400") / 1e6).round(2).alias("net_profit_bln_rub"),
        ])
        .sort("revenue_bln_rub", descending=True)
        .head(10)
        .collect()
    )
    print(top_firms)

    # 3. DFO summary: Activity by OKVED (xx.xx)
    print(f"\n[2] Top 5 OKVED sectors in Far Eastern Federal District (ДФО, year {latest_year}):")
    dfo_okved = (
        lf.filter((pl.col("year") == latest_year) & dfo)
        .with_columns([
            pl.col("okved").str.slice(0, 5).alias("okved_group"),
            pl.col("line_2110").fill_null(0.0).alias("revenue"),
        ])
        .filter(pl.col("okved_group").is_not_null() & (pl.col("okved_group") != ""))
        .group_by("okved_group")
        .agg([
            pl.len().alias("firms_count"),
            (pl.col("revenue").sum() / 1e6).round(2).alias("revenue_bln_rub"),
        ])
        .sort("revenue_bln_rub", descending=True)
        .head(5)
        .collect()
    )
    print(dfo_okved)

    print("\n" + "=" * 60)
    print("Interactive tips:")
    print("  Run in REPL:       uv run python -i explore.py")
    print("  Objects ready:     lf, lf_desc, dfo, pl")
    print("  Run DFO script:    uv run python scripts/dfo_analysis.py --year 2024")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    demo()
