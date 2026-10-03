"""Expressions used by the example scripts; see docs/analysis_rules.md."""

from pathlib import Path

import polars as pl


def finite(column: str) -> pl.Expr:
    value = pl.col(column)
    return pl.when(value.is_finite()).then(value)


def observed_sum(value: pl.Expr) -> pl.Expr:
    """Unlike Polars sum, an entirely missing group stays null."""
    return pl.when(value.count() > 0).then(value.sum())


def read_exclusions(path: Path | None) -> pl.LazyFrame:
    """Read explicit firm-year decisions; a review flag never excludes a firm."""
    schema = {"inn": pl.String, "year": pl.Int64}
    if path is None:
        return pl.DataFrame(schema=schema).lazy()
    df = pl.read_csv(path, schema_overrides=schema)
    required = {"inn", "year", "action", "reason", "source"}
    if not required.issubset(df.columns):
        raise ValueError(f"Decisions require columns: {sorted(required)}")
    invalid = df.filter(
        ~pl.col("action").is_in(["review", "exclude"]).fill_null(False)
        | ~pl.col("inn").str.contains(r"^\d{10}$").fill_null(False)
        | pl.col("year").is_null()
        | ((pl.col("action") == "exclude") & (
            pl.col("reason").fill_null("").str.strip_chars().eq("")
            | pl.col("source").fill_null("").str.strip_chars().eq("")
        ))
    )
    if invalid.height:
        raise ValueError("Invalid decisions: exclusion needs INN, year, reason and source")
    return df.filter(pl.col("action") == "exclude").select("inn", "year").unique().lazy()


COMMERCIAL = pl.col("okopf").str.contains(r"^[16]\d{4}$").fill_null(False)
NONFINANCIAL = (pl.col("financial") == 0).fill_null(False) & (
    (pl.col("okved_section") != "K").fill_null(True)
)
BASE = COMMERCIAL & NONFINANCIAL
CLEAN = (pl.col("outlier") == 0).fill_null(False)
REVENUE = finite("line_2110")
GROSS_PROFIT = pl.when(pl.col("simplified") == 0).then(
    pl.coalesce(finite("line_2100"), REVENUE + finite("line_2120"))
)
SIMPLIFIED_RESULT = pl.when(pl.col("simplified") == 1).then(
    REVENUE + finite("line_2120")
)
BALANCE_GAP = finite("line_1600") - finite("line_1700")
PROFIT_GAP = pl.when(pl.col("simplified") == 0).then(
    finite("line_2100") - REVENUE - finite("line_2120")
)

DIVISION = pl.col("okved").str.extract(r"^(\d{2})(?:\.\d{1,2}){0,2}$", 1).cast(pl.Int32)
SECTION_RANGES = {
    "A": (1, 3), "B": (5, 9), "C": (10, 33), "D": (35, 35),
    "E": (36, 39), "F": (41, 43), "G": (45, 47), "H": (49, 53),
    "I": (55, 56), "J": (58, 63), "K": (64, 66), "L": (68, 68),
    "M": (69, 75), "N": (77, 82), "O": (84, 84), "P": (85, 85),
    "Q": (86, 88), "R": (90, 93), "S": (94, 96), "T": (97, 98), "U": (99, 99),
}
derived_section = pl.lit(None, dtype=pl.String)
for section, (low, high) in SECTION_RANGES.items():
    derived_section = pl.when(DIVISION.is_between(low, high)).then(pl.lit(section)).otherwise(derived_section)
SECTION_CONFLICT = (
    derived_section.is_not_null() & pl.col("okved_section").is_not_null()
    & (derived_section != pl.col("okved_section"))
).fill_null(False)
SECTION = (
    pl.when(SECTION_CONFLICT).then(pl.lit("conflict"))
    .when(derived_section.is_null() | pl.col("okved_section").is_null()).then(pl.lit("unknown"))
    .otherwise(derived_section)
)
OKVED_CLASS = pl.when(SECTION.is_in(list(SECTION_RANGES))).then(
    pl.col("okved").str.extract(r"^(\d{2}\.\d{2})(?:\.\d{1,2})?$", 1)
)
CLUSTER = (
    pl.when(SECTION == "conflict").then(pl.lit("Конфликт ОКВЭД"))
    .when(SECTION == "unknown").then(pl.lit("ОКВЭД неизвестен"))
    .when(DIVISION.is_between(5, 9) | DIVISION.is_in([20, 24])).then(pl.lit("Недропользование и сырьё"))
    .when((DIVISION == 3) | pl.col("okved").str.starts_with("10.2")).then(pl.lit("Рыбный комплекс"))
    .when(DIVISION == 10).then(pl.lit("Пищепром"))
    .when(DIVISION == 1).then(pl.lit("Сельское хозяйство"))
    .when(DIVISION == 2).then(pl.lit("Лесное хозяйство"))
    .when(DIVISION.is_between(49, 53)).then(pl.lit("Транспорт и логистика"))
    .when((DIVISION == 23) | DIVISION.is_between(35, 39) | DIVISION.is_between(41, 43))
    .then(pl.lit("Строительство, энергетика и коммунальные виды"))
    .when(DIVISION.is_between(45, 47) | DIVISION.is_in([55, 56, 68])).then(pl.lit("Торговля и потребсектор"))
    .otherwise(pl.lit("Прочее"))
)

SUMMARY = [
    pl.len().alias("firms"),
    ((pl.col("filed") == 1) | (pl.col("imputed") == 1)).sum().alias("with_statements"),
    (pl.col("imputed") == 1).sum().alias("imputed"),
    REVENUE.count().alias("revenue_observed"),
    (REVENUE > 0).sum().alias("revenue_positive"),
    (REVENUE < 0).sum().alias("revenue_negative"),
    (observed_sum(REVENUE) / 1e6).alias("revenue_bln"),
    GROSS_PROFIT.count().alias("gross_profit_observed"),
    (observed_sum(GROSS_PROFIT) / 1e6).alias("gross_profit_full_bln"),
    SIMPLIFIED_RESULT.count().alias("simplified_result_observed"),
    (observed_sum(SIMPLIFIED_RESULT) / 1e6).alias("simplified_result_bln"),
    (BALANCE_GAP.abs() > 1).sum().alias("balance_mismatch"),
    (PROFIT_GAP.abs() > 1).sum().alias("profit_mismatch"),
]

positive_revenue = REVENUE.filter(REVENUE > 0)
for n in [5, 10]:
    SUMMARY.append(
        pl.when(positive_revenue.sum() > 0)
        .then(positive_revenue.sort(descending=True).head(n).sum() / positive_revenue.sum())
        .alias(f"top{n}_revenue_share")
    )
for name, profit in [("gross_profit", GROSS_PROFIT), ("simplified_result", SIMPLIFIED_RESULT)]:
    SUMMARY.append(
        pl.when(positive_revenue.sum() > 0)
        .then(REVENUE.filter((REVENUE > 0) & profit.is_not_null()).sum() / positive_revenue.sum())
        .alias(f"{name}_revenue_share")
    )
