"""DFO activity and head firms by OKVED class; base and clean samples."""

import argparse
from pathlib import Path

import polars as pl

from firm_level import dfo_filter, region_code, scan_rfsd
from firm_level.analysis import (
    BASE, CLEAN, CLUSTER, OKVED_CLASS, REVENUE, SECTION, SUMMARY,
    read_exclusions,
)
from firm_level.results import create_run, finish_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--top-sectors", type=int, default=15)
    parser.add_argument("--top-firms", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, help="New directory; existing runs are never overwritten")
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    if min(args.top_sectors, args.top_firms) < 1:
        parser.error("top-sectors and top-firms must be positive")
    args.output_dir = create_run("dfo", args.output_dir)
    base = scan_rfsd(year=args.year).filter(dfo_filter() & BASE).with_columns(
        region_code().alias("region_code"), OKVED_CLASS.alias("okved_class"),
        SECTION.alias("section"), CLUSTER.alias("cluster"), REVENUE.alias("revenue"),
    )
    clean = base.filter(CLEAN).join(read_exclusions(args.decisions), on=["inn", "year"], how="anti")
    for name, lf in [("base", base), ("clean", clean)]:
        activity, regions, clusters = pl.collect_all([
            lf.group_by("year", "section", "okved_class").agg(SUMMARY)
              .sort("revenue_bln", descending=True, nulls_last=True),
            lf.group_by("year", "region_code", "section", "okved_class").agg(SUMMARY)
              .sort("year", "region_code", "section", "okved_class"),
            lf.group_by("year", "cluster").agg(SUMMARY).sort("year", "cluster"),
        ])
        top = activity.filter(pl.col("okved_class").is_not_null()).head(args.top_sectors)["okved_class"]
        heads = (
            lf.filter(pl.col("okved_class").is_in(top.to_list()) & (pl.col("revenue") > 0))
            .select("year", "inn", "ogrn", "region_code", "okved", "okved_class", "cluster",
                    "revenue", "outlier", "eligible", "imputed")
            .sort(["okved_class", "revenue", "inn"], descending=[False, True, False])
            .group_by("okved_class", maintain_order=True).head(args.top_firms)
            .collect()
        )
        for table_name, table in [("activity", activity), ("regions", regions),
                                  ("clusters", clusters), ("heads", heads)]:
            table.write_csv(args.output_dir / f"{table_name}_{args.year}_{name}.csv")
            # Parquet preserves INN and codes as strings when shared with analysts.
            table.write_parquet(args.output_dir / f"{table_name}_{args.year}_{name}.parquet")
    finish_run(args.output_dir, "dfo", {"year": args.year, "top_sectors": args.top_sectors,
               "top_firms": args.top_firms, "manual_exclusions": read_exclusions(args.decisions).collect().to_dicts(),
               "hashes_verified": False})
    print(f"Wrote and indexed activity and head firms: {args.output_dir}")


if __name__ == "__main__":
    main()
