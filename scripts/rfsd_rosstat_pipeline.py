"""Basic nominal comparisons; no fitted proxy, implicit deflator or cached aggregates."""

import argparse
from pathlib import Path

import polars as pl

from firm_level import dfo_filter, region_code, scan_rfsd
from firm_level.analysis import BASE, CLEAN, SECTION, SUMMARY, read_exclusions
from firm_level.dataset import DATA_DIR
from firm_level.rosstat import read_grp, read_gva
from firm_level.sources import SOURCES, verify_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/comparison"))
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    for name in ["VRP_s1998.xlsx", "VRP_OKVED2_s_2016.xlsx"]:
        verify_file(args.data_dir / name, SOURCES["rosstat"][name]["sha256"])
    grp = read_grp(args.data_dir / "VRP_s1998.xlsx")
    gva = read_gva(args.data_dir / "VRP_OKVED2_s_2016.xlsx")
    grp_total = grp.group_by("year").agg(pl.col("grp_bln").sum())
    check = gva.group_by("year").agg(pl.col("gva_bln").sum()).join(grp_total, on="year")
    if check.filter((pl.col("gva_bln") - pl.col("grp_bln")).abs() > 0.001).height:
        raise ValueError("GVA and GRP source totals disagree")
    base = scan_rfsd(year=list(range(2011, 2026)), data_dir=args.data_dir / "RFSD").filter(dfo_filter() & BASE).with_columns(
        SECTION.alias("section"), region_code().alias("region_code"),
    )
    clean = base.filter(CLEAN).join(read_exclusions(args.decisions), on=["inn", "year"], how="anti")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, lf in [("base", base), ("clean", clean)]:
        macro, sectors, regions = pl.collect_all([
            lf.group_by("year").agg(SUMMARY),
            lf.group_by("year", "section").agg(SUMMARY),
            lf.group_by("year", "region_code").agg(SUMMARY),
        ])
        macro = macro.join(grp_total, on="year", how="full", coalesce=True).with_columns(
            (pl.col("revenue_bln") / pl.col("grp_bln")).alias("revenue_grp_ratio"),
        ).sort("year")
        sectors = sectors.join(gva, on=["year", "section"], how="full", coalesce=True).with_columns(
            (pl.col("revenue_bln") / pl.col("gva_bln")).alias("revenue_gva_ratio"),
        ).sort("year", "section")
        regions = regions.join(grp, on=["year", "region_code"], how="full", coalesce=True).sort("year", "region_code")
        for table_name, table in [("macro", macro), ("sectors", sectors), ("regions", regions)]:
            table.write_csv(args.output_dir / f"{table_name}_{name}.csv")
    print(f"Wrote nominal reference tables: {args.output_dir}. Ratios are not coverage estimates.")


if __name__ == "__main__":
    main()
