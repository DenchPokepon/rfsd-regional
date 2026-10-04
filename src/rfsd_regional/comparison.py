"""Descriptive growth and correlation checks; no fitted GRP proxy."""

import math

import polars as pl

KEYS = ["sample", "dimension", "group", "metric"]


def add_growth(levels: pl.DataFrame, volumes: pl.DataFrame) -> pl.DataFrame:
    if levels.unique([*KEYS, "year"]).height != levels.height:
        raise ValueError("Duplicate comparison keys")
    previous = levels.select(*KEYS, (pl.col("year") + 1).alias("year"),
                             pl.col("rfsd_bln").alias("previous_rfsd_bln"),
                             pl.col("official_bln").alias("previous_official_bln"))
    result = levels.join(previous, on=[*KEYS, "year"], how="left", validate="1:1").with_columns(
        pl.when(pl.col("previous_rfsd_bln") > 0).then(
            pl.col("rfsd_bln") / pl.col("previous_rfsd_bln") - 1
        ).alias("rfsd_growth"),
        pl.when(pl.col("previous_official_bln") > 0).then(
            pl.col("official_bln") / pl.col("previous_official_bln") - 1
        ).alias("official_growth"),
        (pl.col("rfsd_bln") - pl.col("previous_rfsd_bln")).alias("rfsd_change_bln"),
    ).join(volumes.select("year", "dimension", "group", "volume_factor"),
           on=["year", "dimension", "group"], how="left", validate="m:1")
    return add_deflated_growth(result)


def add_deflated_growth(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.when((pl.col("volume_factor") > 0) & (pl.col("official_growth") > -1)).then(
            (1 + pl.col("official_growth")) / pl.col("volume_factor")
        ).alias("implicit_deflator_factor"),
        pl.when(pl.col("volume_factor") > 0).then(pl.col("volume_factor") - 1).alias("official_real_growth"),
    ).with_columns(
        ((1 + pl.col("rfsd_growth")) / pl.col("implicit_deflator_factor") - 1)
        .alias("rfsd_deflated_growth"),
    )


def correlations(growth: pl.DataFrame) -> pl.DataFrame:
    """Pearson on finite pairs, 2017–2024; exclusions expose small-sample sensitivity."""
    rows = []
    period = growth.filter(pl.col("year").is_between(2017, 2024))
    for key, frame in period.group_by(KEYS):
        for mode, x, y in [("nominal", "rfsd_growth", "official_growth"),
                           ("target_implicit_deflator", "rfsd_deflated_growth", "official_real_growth")]:
            pairs = frame.filter(pl.col(x).is_finite() & pl.col(y).is_finite())
            exclusions = {"all": [], "without_2020_2021": [2020, 2021],
                          **{f"without_{year}": [year] for year in range(2017, 2025)}}
            for scenario, omitted in exclusions.items():
                df = pairs.filter(~pl.col("year").is_in(omitted))
                r = None
                if df.height >= 3 and df[x].n_unique() > 1 and df[y].n_unique() > 1:
                    value = df.select(pl.corr(x, y)).item()
                    r = value if value is not None and math.isfinite(value) else None
                rows.append(dict(zip(KEYS, key)) | {
                    "mode": mode, "scenario": scenario, "n": df.height, "pearson_r": r,
                    "years": ",".join(map(str, sorted(df["year"].to_list()))),
                })
    return pl.DataFrame(rows, schema_overrides={"pearson_r": pl.Float64}).sort(*KEYS, "mode", "scenario")
