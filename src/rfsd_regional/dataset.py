"""Local RFSD access. scan_rfsd returns an unfiltered Polars LazyFrame."""

import os
from collections.abc import Sequence
from pathlib import Path

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("RFSD_DATA_DIR", PROJECT_ROOT / "data"))
RFSD_DIR = DATA_DIR / "RFSD"
NAMES_DICT_PATH = Path(__file__).with_name("descriptive_names.csv")

# tax region -> (RFSD region label, Rosstat region label)
DFO = {
    "0300": ("buryatia", "Республика Бурятия"),
    "1400": ("yakutia", "Республика Саха (Якутия)"),
    "2500": ("primorsky terr.", "Приморский край"),
    "2700": ("khabarovsk", "Хабаровский край"),
    "2800": ("amur", "Амурская область"),
    "4100": ("kamchatka", "Камчатский край"),
    "4900": ("magadan", "Магаданская область"),
    "6500": ("sakhalin", "Сахалинская область"),
    "7500": ("zabaikalsk terr.", "Забайкальский край"),
    "7900": ("jewish ao", "Еврейская автономная область"),
    "8700": ("chukotka", "Чукотский автономный округ"),
}
DFO_TAXCODES = list(DFO)
DFO_REGIONS = [names[0] for names in DFO.values()]


def region_code() -> pl.Expr:
    """Tax region takes precedence; only an empty code permits label fallback."""
    code = pl.col("region_taxcode").str.strip_chars()
    return (
        pl.when(code.is_not_null() & (code != ""))
        .then(pl.when(code.str.contains(r"^\d{4}$")).then(code))
        .otherwise(pl.col("region").replace_strict(
            {names[0]: tax for tax, names in DFO.items()}, default=None,
        ))
    )


def dfo_filter() -> pl.Expr:
    return region_code().is_in(DFO_TAXCODES).fill_null(False)


def load_renaming_dict() -> dict[str, str]:
    df = pl.read_csv(NAMES_DICT_PATH)
    return dict(zip(df["original"], df["descriptive"]))


def scan_rfsd(
    year: int | Sequence[int] | None = None,
    descriptive: bool = False,
    data_dir: Path | str | None = None,
) -> pl.LazyFrame:
    """Read all downloaded years, or exactly the requested partitions."""
    base = Path(data_dir) if data_dir is not None else RFSD_DIR
    if year is None:
        files = sorted(base.glob("year=*/*.parquet"))
    else:
        years = [year] if type(year) is int else year
        if (not isinstance(years, Sequence) or isinstance(years, (str, bytes))
                or not years or any(type(y) is not int for y in years)):
            raise TypeError("year must be an int or a nonempty sequence of ints")
        files = []
        for y in sorted(set(years)):
            partition = sorted((base / f"year={y}").glob("*.parquet"))
            if not partition:
                raise FileNotFoundError(f"Missing RFSD year={y} in {base}")
            files.extend(partition)
    if not files:
        raise FileNotFoundError(
            f"No RFSD partitions in {base}. Run uv run --group download python scripts/download_data.py"
        )
    lf = pl.scan_parquet(files, hive_partitioning=True)
    if descriptive:
        names = lf.collect_schema().names()
        lf = lf.rename({k: v for k, v in load_renaming_dict().items() if k in names})
    return lf
