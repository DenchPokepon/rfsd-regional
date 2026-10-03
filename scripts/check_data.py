"""DFO coverage, cleaning sensitivity and adjacent-year diagnostics from raw Parquet."""

import argparse
import json
from pathlib import Path

import polars as pl

from firm_level import dfo_filter, scan_rfsd
from firm_level.dataset import DFO, RFSD_DIR, region_code
from firm_level.analysis import (
    BASE, CLEAN, REVENUE, SUMMARY, SECTION, CLUSTER, BALANCE_GAP, PROFIT_GAP,
    observed_sum, finite, read_exclusions,
)
from firm_level.diagnostics import AUDIT_METRICS, BUCKET, adjacent_panel, anomaly_candidates, changes
from firm_level.sources import SOURCES, verify_file
from firm_level.results import create_run, finish_run

FIELDS = ["line_1100", "line_1200", "line_1230", "line_1240", "line_1300", "line_1600",
          "line_1700", "line_2100", "line_2110", "line_2120", "line_2200", "line_2400"]
COLUMNS = ["inn", "year", "region", "region_taxcode", "okopf", "okved", "okved_section",
           "eligible", "financial", "outlier", "filed", "imputed", "simplified", "articulated"] + FIELDS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+")
    parser.add_argument("--output-dir", type=Path, help="New directory; existing runs are never overwritten")
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--top", type=int, default=20, help="Candidates per year and reason")
    parser.add_argument("--trace-inn", nargs="+", default=[], help="Optional firm histories, across all Russia")
    args = parser.parse_args()
    if args.top < 1:
        parser.error("--top must be positive")
    years = sorted(set(args.years or [int(p.name[5:]) for p in RFSD_DIR.glob("year=*") if p.is_dir()]))
    if not years:
        raise FileNotFoundError(f"No RFSD partitions in {RFSD_DIR}")
    args.output_dir = create_run("audit", args.output_dir)
    exclusions = read_exclusions(args.decisions).collect().with_columns(pl.lit(True).alias("manual_exclusion"))
    tables = {name: [] for name in ["coverage", "groups", "flags", "selection", "forms", "geography",
                                    "anomalies", "adjacent_growth", "changes", "transitions", "hospital",
                                    "key_issues", "geography_issues", "case_history"]}
    previous = None
    for year in years:
        files = sorted((RFSD_DIR / f"year={year}").glob("*.parquet"))
        for path in files:
            source = SOURCES["rfsd"]["files"].get(f"RFSD/year={year}/{path.name}")
            if source is None:
                raise ValueError(f"Unpinned source: {path}")
            verify_file(path, source["sha256"])
        raw = scan_rfsd(year)
        if args.trace_inn:
            tables["case_history"].append(raw.filter(pl.col("inn").is_in(args.trace_inn)).select(COLUMNS).collect())
        tax = pl.col("region_taxcode").str.strip_chars()
        label_dfo = pl.col("region").is_in([names[0] for names in DFO.values()]).fill_null(False)
        label_code = pl.col("region").replace_strict({names[0]: code for code, names in DFO.items()}, default=None)
        geo = raw.select(
            pl.len().alias("russia_rows"), pl.col("inn").n_unique().alias("russia_unique_inn"),
            pl.col("inn").null_count().alias("inn_missing"),
            (~pl.col("inn").str.contains(r"^\d{10}$").fill_null(False)).sum().alias("inn_invalid"),
            (pl.col("year") != year).sum().alias("wrong_partition_year"),
            (tax.is_null() | (tax == "")).sum().alias("russia_empty_taxcode"),
            (tax.is_not_null() & (tax != "") & ~tax.str.contains(r"^\d{4}$")).sum().alias("russia_malformed_taxcode"),
            dfo_filter().sum().alias("dfo_rows"),
            (dfo_filter() & (tax.is_null() | (tax == ""))).sum().alias("dfo_label_fallback"),
            (dfo_filter() != label_dfo).sum().alias("dfo_membership_disagreement"),
            ((dfo_filter() | label_dfo) & label_code.is_not_null() & (region_code() != label_code))
            .sum().alias("dfo_known_region_disagreement"),
            (dfo_filter() & pl.col("region").is_null()).sum().alias("dfo_missing_label"),
        ).collect().with_columns(pl.lit(year).alias("year"))
        check = geo.row(0, named=True)
        if check["wrong_partition_year"]:
            raise ValueError(f"Wrong partition year: {check}")
        if check["russia_rows"] != check["russia_unique_inn"] or check["inn_invalid"]:
            tables["key_issues"].append(raw.select(COLUMNS).filter(
                ~pl.col("inn").str.contains(r"^\d{10}$").fill_null(False) | pl.col("inn").is_duplicated()
            ).collect())
        if check["dfo_membership_disagreement"] or check["dfo_known_region_disagreement"]:
            tables["geography_issues"].append(raw.select(COLUMNS).filter(
                (dfo_filter() != label_dfo)
                | ((dfo_filter() | label_dfo) & (region_code() != label_code))
            ).with_columns(dfo_filter().alias("included_dfo"), BASE.alias("base")).collect())
        tables["geography"].append(geo)
        df = raw.filter(dfo_filter()).select(COLUMNS).with_columns(
            region_code().alias("region_code"), SECTION.alias("section"), CLUSTER.alias("cluster"),
            BASE.alias("base"), REVENUE.alias("revenue"),
            BALANCE_GAP.alias("balance_gap"), PROFIT_GAP.alias("profit_gap"),
        ).collect().join(exclusions, on=["inn", "year"], how="left").with_columns(
            pl.col("manual_exclusion").fill_null(False)
        ).with_columns((BASE & CLEAN & ~pl.col("manual_exclusion")).alias("clean"), BUCKET.alias("bucket"))
        if df["inn"].n_unique() != df.height or df.filter(
            ~pl.col("inn").str.contains(r"^\d{10}$").fill_null(False)
        ).height:
            raise ValueError(f"Invalid firm-year keys inside DFO in {year}")
        for sample, subset in [("all_dfo", df), ("base", df.filter("base")), ("clean", df.filter("clean"))]:
            tables["coverage"].append(subset.group_by("year").agg(SUMMARY + AUDIT_METRICS)
                                      .with_columns(pl.lit(sample).alias("sample")))
            for dimension in ["section", "cluster", "region_code"]:
                tables["groups"].append(subset.group_by("year", dimension).agg(SUMMARY + AUDIT_METRICS)
                                        .rename({dimension: "group"}).with_columns(
                                            pl.lit(dimension).alias("dimension"), pl.lit(sample).alias("sample")))
            tables["forms"].append(subset.group_by("year", "simplified").agg(
                pl.len().alias("firms"),
                *[finite(col).count().alias(f"{col}_observed") for col in FIELDS],
                *[(finite(col) == 0).sum().alias(f"{col}_zero") for col in FIELDS],
            ).with_columns(pl.lit(sample).alias("sample")))
        measures = [pl.len().alias("firms"), REVENUE.count().alias("revenue_observed"),
                    (observed_sum(REVENUE) / 1e6).alias("revenue_bln")]
        tables["flags"].append(df.group_by("year", "bucket", "eligible", "financial", "outlier", "filed", "imputed")
                               .agg(measures))
        tables["selection"].append(df.group_by("year", "cluster", "bucket").agg(measures))
        tables["anomalies"].append(anomaly_candidates(df, args.top))
        tables["hospital"].append(df.filter(pl.col("inn") == "2526003992"))
        if previous is not None and year == previous["year"][0] + 1:
            for sample in ["base", "clean"]:
                for dimension in ["total", "section", "cluster"]:
                    tables["adjacent_growth"].append(adjacent_panel(previous.filter(sample), df.filter(sample), dimension)
                                                     .with_columns(pl.lit(year).alias("year"), pl.lit(sample).alias("sample")))
            delta = changes(previous, df)
            tables["transitions"].append(delta.group_by(
                "base_previous", "base", "clean_previous", "clean",
                pl.col("revenue_previous").is_not_null().alias("observed_previous"),
                pl.col("revenue").is_not_null().alias("observed_current"),
                (pl.col("region_code") != pl.col("region_code_previous")).alias("region_changed"),
                (pl.col("section") != pl.col("section_previous")).alias("section_changed"),
            ).agg(pl.len().alias("firms"),
                  (observed_sum(pl.col("revenue_previous")) / 1e6).alias("previous_bln"),
                  (observed_sum(pl.col("revenue")) / 1e6).alias("current_bln"))
                .with_columns(pl.lit(year).alias("year")))
            for reason, condition, score in [
                ("largest_change", pl.col("revenue_change").is_not_null(), pl.col("revenue_change").abs()),
                ("newly_observed", pl.col("revenue_previous").is_null(), pl.col("revenue").abs()),
                ("no_longer_observed", pl.col("revenue").is_null(), pl.col("revenue_previous").abs()),
                ("scale_jump", (pl.col("revenue_ratio").is_between(800, 1200)
                                | pl.col("revenue_ratio").is_between(1/1200, 1/800)),
                 pl.col("revenue_change").abs()),
            ]:
                tables["changes"].append(delta.filter(condition).with_columns(score.alias("score_thousand"))
                                         .filter(pl.col("score_thousand") > 0)
                                         .sort(["score_thousand", "inn"], descending=[True, False]).head(args.top)
                                         .with_columns(pl.lit(reason).alias("reason"), pl.lit(year).alias("comparison_year")))
        previous = df
        print(f"Checked {year}: {df.height:,} DFO firm-years", flush=True)
    for name, frames in tables.items():
        if frames:
            result = pl.concat(frames, how="vertical_relaxed")
            sort_cols = [col for col in ["year", "comparison_year", "sample", "dimension", "group", "reason", "inn",
                                          "cluster", "bucket", "simplified", "eligible", "financial", "outlier", "filed", "imputed"]
                         if col in result.columns]
            result = result.sort(sort_cols, nulls_last=True)
            result.write_csv(args.output_dir / f"{name}.csv")
            result.write_parquet(args.output_dir / f"{name}.parquet")
    metadata = {"years": years, "rfsd_revision": SOURCES["rfsd"]["revision"], "hashes_verified": True,
                "top_per_reason_year": args.top, "trace_inn": args.trace_inn,
                "manual_exclusions": exclusions.select("inn", "year").to_dicts()}
    (args.output_dir / "run.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    finish_run(args.output_dir, "audit", metadata)
    print(f"Wrote {args.output_dir}")


if __name__ == "__main__":
    main()
