"""
Command-line interface for firm-level.
Simple, fast, laconic.
"""
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path
import polars as pl
from firm_level.dataset import (
    RFSD_DIR,
    DATA_DIR,
    scan_rfsd,
    load_renaming_dict,
    dfo_filter,
    DFO_REGIONS,
)


def cmd_status(args):
    """Print current dataset download status and basic stats."""
    parquet_files = sorted(RFSD_DIR.glob("year=*/part-0.parquet"))
    print("\n=== RFSD Local Dataset Status ===")
    print(f"Data directory: {RFSD_DIR}")
    print(f"Total partitions found: {len(parquet_files)} / 15 years (2011-2025)")
    print("-" * 50)
    
    total_bytes = 0
    for f in parquet_files:
        size_mb = f.stat().st_size / (1024 * 1024)
        total_bytes += f.stat().st_size
        print(f"  {f.parent.name:<12} | {size_mb:>7.1f} MB | {f.name}")
    print("-" * 50)
    print(f"Total disk space used: {total_bytes / (1024**3):.2f} GB")

    if parquet_files:
        print("\nScanning partition metadata via Polars...")
        lf = scan_rfsd()
        schema = lf.collect_schema()
        print(f"Columns: {len(schema)} available in schema")
        year_counts = (
            lf.group_by("year")
            .agg(pl.len().alias("firms_count"))
            .sort("year")
            .collect()
        )
        print("\nFirms count per downloaded year:")
        print(year_counts)


def cmd_explore(args):
    """Launch interactive Python REPL with LazyFrame pre-loaded."""
    print("Pre-loading RFSD into Polars LazyFrame...")
    print("Variables available in session:")
    print("  pl       : Polars module")
    print("  lf       : Raw LazyFrame (line_XXXX column names)")
    print("  lf_desc  : Descriptive LazyFrame (B_revenue, PL_net_profit, etc.)")
    print("  dfo      : Filter expression for Far East District (dfo_filter())")
    print("Example: lf.filter(dfo).select(['year', 'inn', 'line_2110']).head(10).collect()\n")

    import code
    session_env = {
        "pl": pl,
        "scan_rfsd": scan_rfsd,
        "dfo_filter": dfo_filter,
        "dfo": dfo_filter(),
        "lf": scan_rfsd(),
        "lf_desc": scan_rfsd(descriptive=True),
        "DFO_REGIONS": DFO_REGIONS,
    }
    code.interact(local=session_env)


def cmd_dfo(args):
    """Run DFO regional analysis."""
    from scripts.dfo_analysis import analyze_dfo
    analyze_dfo(year=args.year, top_k_sectors=args.top_sectors, top_k_firms=args.top_firms)


def main():
    parser = argparse.ArgumentParser(
        prog="firm-level",
        description="Laconic SOTA working environment for RFSD firm-level analytics",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Status command
    subparsers.add_parser("status", help="Show downloaded dataset partitions and stats")

    # Explore command
    subparsers.add_parser("explore", help="Start interactive Python REPL with LazyFrame pre-loaded")

    # DFO analysis command
    dfo_parser = subparsers.add_parser("dfo", help="Run DFO (Far East) regional & OKVED analysis")
    dfo_parser.add_argument("--year", type=int, default=2024, help="Year to analyze (default: 2024)")
    dfo_parser.add_argument("--top-sectors", type=int, default=15, help="Number of top sectors")
    dfo_parser.add_argument("--top-firms", type=int, default=10, help="Number of top firms per sector")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "explore":
        cmd_explore(args)
    elif args.command == "dfo":
        cmd_dfo(args)
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
