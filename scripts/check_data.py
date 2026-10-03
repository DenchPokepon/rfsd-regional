"""Basic DFO coverage and flags; recalculated from Parquet, without a cache."""

import argparse
from pathlib import Path

import polars as pl

from firm_level import dfo_filter, scan_rfsd
from firm_level.analysis import BASE, CLEAN, REVENUE, SUMMARY, observed_sum, read_exclusions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/checks"))
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    raw = scan_rfsd(year=args.years).filter(dfo_filter())
    keys = raw.select(pl.len().alias("n"), pl.struct("inn", "year").n_unique().alias("unique"),
                      pl.col("inn").null_count().alias("missing")).collect().row(0)
    if keys[0] != keys[1] or keys[2]:
        raise ValueError("RFSD has missing or duplicate firm-year keys in the requested DFO sample")
    base = raw.filter(BASE)
    clean = base.filter(CLEAN).join(read_exclusions(args.decisions), on=["inn", "year"], how="anti")
    frames = pl.collect_all([
        lf.group_by("year").agg(SUMMARY).with_columns(pl.lit(name).alias("sample"))
        for name, lf in [("all_dfo", raw), ("base", base), ("clean", clean)]
    ])
    summary = pl.concat(frames).sort("year", "sample")
    flags = (
        raw.group_by("year", pl.col("okopf").str.slice(0, 1).alias("okopf_prefix"),
                     "eligible", "financial", "outlier")
        .agg(pl.len().alias("firms"), REVENUE.count().alias("revenue_observed"),
             (observed_sum(REVENUE) / 1e6).alias("revenue_bln"))
        .sort("year", "okopf_prefix", "eligible", "financial", "outlier").collect()
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary.write_csv(args.output_dir / "coverage.csv")
    flags.write_csv(args.output_dir / "flags.csv")
    print(f"Checked keys and coverage; wrote {args.output_dir}")


if __name__ == "__main__":
    main()
