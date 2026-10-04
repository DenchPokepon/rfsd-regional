"""Explicit queries for the data audit; no corrections to source values."""

import polars as pl

from rfsd_regional.analysis import (
    BASE, CLEAN, COMMERCIAL, NONFINANCIAL, BALANCE_GAP, PROFIT_GAP, REVENUE,
    finite, observed_sum,
)

OKOPF_STATUS = (
    pl.when(COMMERCIAL).then(pl.lit("commercial"))
    .when(pl.col("okopf").str.contains(r"^[27]\d{4}$")).then(pl.lit("noncommercial"))
    .otherwise(pl.lit("other_or_unknown"))
)
BUCKET = (
    pl.when(OKOPF_STATUS != "commercial").then(OKOPF_STATUS)
    .when((pl.col("financial") == 1) | (pl.col("okved_section") == "K"))
    .then(pl.lit("financial"))
    .when(~NONFINANCIAL).then(pl.lit("financial_unknown"))
    .when(pl.col("outlier") == 1).then(pl.lit("outlier"))
    .when(~CLEAN).then(pl.lit("outlier_unknown"))
    .when(pl.col("manual_exclusion")).then(pl.lit("manual_exclusion"))
    .otherwise(pl.lit("clean"))
)

AUDIT_METRICS = [
    BALANCE_GAP.count().alias("balance_checkable"),
    PROFIT_GAP.count().alias("profit_checkable"),
    (~pl.col("line_2110").is_finite() & pl.col("line_2110").is_not_null()).sum().alias("revenue_nonfinite"),
    (finite("line_2120") > 0).sum().alias("positive_costs"),
    (pl.col("eligible") == 0).sum().alias("ineligible"),
    (~pl.col("simplified").is_in([0, 1]).fill_null(False)).sum().alias("form_unknown"),
    (REVENUE.is_not_null() & finite("line_2120").is_null()).sum().alias("revenue_without_costs"),
]
for label, condition in {
    "imputed": pl.col("imputed") == 1,
    "filed": pl.col("filed") == 1,
    "ineligible": pl.col("eligible") == 0,
    "unarticulated": pl.col("articulated") == 0,
    "negative": REVENUE < 0,
}.items():
    AUDIT_METRICS.append((observed_sum(REVENUE.filter(condition)) / 1e6).alias(f"{label}_revenue_bln"))


def adjacent_panel(previous: pl.DataFrame, current: pl.DataFrame, group: str) -> pl.DataFrame:
    """Both inputs are one year of one sample. Missing revenue never becomes zero."""
    keys = ["inn", "region_code"] + ([] if group == "total" else [group])
    columns = list(dict.fromkeys(keys + ["year", "revenue", "imputed"]))
    before = previous.filter(pl.col("revenue").is_not_null()).select(columns)
    after = current.filter(pl.col("revenue").is_not_null()).select(columns)
    paired = after.join(before, on=keys, how="inner", suffix="_previous", validate="1:1").filter(
        pl.col("year") == pl.col("year_previous") + 1
    )
    if group == "section":
        paired = paired.filter(~pl.col("section").is_in(["unknown", "conflict"]))
    elif group == "cluster":
        paired = paired.filter(~pl.col("cluster").is_in(["ОКВЭД неизвестен", "Конфликт ОКВЭД"]))
    grouped = paired.with_columns(pl.lit("DFO").alias("total")).group_by(group).agg(
        pl.len().alias("panel_firms"),
        (observed_sum(pl.col("revenue_previous")) / 1e6).alias("panel_previous_bln"),
        (observed_sum(pl.col("revenue")) / 1e6).alias("panel_current_bln"),
        ((pl.col("imputed") == 1) | (pl.col("imputed_previous") == 1)).sum().alias("panel_imputed_either_year"),
    )
    sums = []
    for label, df in [("previous", before), ("current", after)]:
        sums.append(df.with_columns(pl.lit("DFO").alias("total")).group_by(group).agg(
            pl.len().alias(f"observed_{label}"),
            (observed_sum(pl.col("revenue")) / 1e6).alias(f"aggregate_{label}_bln"),
        ))
    result = sums[1].join(sums[0], on=group, how="full", coalesce=True).join(grouped, on=group, how="left")
    return result.with_columns(
        pl.when(pl.col("panel_previous_bln") > 0).then(
            pl.col("panel_current_bln") / pl.col("panel_previous_bln") - 1
        ).alias("panel_growth"),
        pl.when(pl.col("aggregate_previous_bln") > 0).then(
            pl.col("aggregate_current_bln") / pl.col("aggregate_previous_bln") - 1
        ).alias("aggregate_growth"),
        (pl.col("aggregate_current_bln") - pl.col("panel_current_bln")).alias("outside_panel_current_bln"),
        (pl.col("aggregate_previous_bln") - pl.col("panel_previous_bln")).alias("outside_panel_previous_bln"),
    ).rename({group: "group"}).with_columns(pl.lit(group).alias("dimension"))


def anomaly_candidates(df: pl.DataFrame, limit: int) -> pl.DataFrame:
    """Top N per reason and year, ranked by amount; these are review candidates."""
    reasons = {
        "outlier": (pl.col("outlier") == 1, REVENUE.abs()),
        "unknown_okopf": (OKOPF_STATUS == "other_or_unknown", REVENUE.abs()),
        "negative_revenue": (REVENUE < 0, REVENUE.abs()),
        "positive_costs": (finite("line_2120") > 0, finite("line_2120")),
        "balance_gap": (BALANCE_GAP.abs() > 1, BALANCE_GAP.abs()),
        "profit_gap": (PROFIT_GAP.abs() > 1, PROFIT_GAP.abs()),
        "missing_revenue_large_assets": (REVENUE.is_null(), finite("line_1600").abs()),
        "large_unflagged": (BASE & CLEAN, REVENUE.abs()),
    }
    rows = []
    for reason, (condition, score) in reasons.items():
        rows.append(df.filter(condition).with_columns(score.alias("score_thousand"))
                    .filter(pl.col("score_thousand") > 0)
                    .sort(["score_thousand", "inn"], descending=[True, False]).head(limit)
                    .with_columns(pl.lit(reason).alias("reason")))
    return pl.concat(rows)


def changes(previous: pl.DataFrame, current: pl.DataFrame) -> pl.DataFrame:
    """Outer join within DFO: absence means outside this extract, not firm exit."""
    cols = ["inn", "year", "region_code", "okved", "section", "cluster", "revenue",
            "base", "clean", "filed", "imputed", "outlier", "simplified"]
    return current.select(cols).join(previous.select(cols), on="inn", how="full", coalesce=True,
                                     suffix="_previous", validate="1:1").with_columns(
        (pl.col("revenue") - pl.col("revenue_previous")).alias("revenue_change"),
        pl.when(pl.col("revenue_previous") > 0).then(
            pl.col("revenue") / pl.col("revenue_previous")
        ).alias("revenue_ratio"),
    )
