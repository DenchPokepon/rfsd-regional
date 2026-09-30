"""
RFSD Data Access Module.
Pure, native Polars LazyFrame interface with zero bloat.
"""
from pathlib import Path
from typing import Sequence
import polars as pl

# Project root and data directories
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RFSD_DIR = DATA_DIR / "RFSD"
NAMES_DICT_PATH = DATA_DIR / "descriptive_names_dict.csv"

# Far Eastern Federal District (ДФО) identifiers
DFO_REGIONS = [
    "amur",
    "buryatia",
    "jewish ao",
    "zabaikalsk terr.",
    "kamchatka",
    "magadan",
    "primorsky terr.",
    "sakhalin",
    "yakutia",
    "khabarovsk",
    "chukotka",
]

DFO_TAXCODES = [
    "0300",  # Республика Бурятия
    "1400",  # Республика Саха (Якутия)
    "2500",  # Приморский край
    "2700",  # Хабаровский край
    "2800",  # Амурская область
    "4100",  # Камчатский край
    "4900",  # Магаданская область
    "6500",  # Сахалинская область
    "7500",  # Забайкальский край
    "7900",  # Еврейская автономная область
    "8700",  # Чукотский автономный округ
]


def load_renaming_dict() -> dict[str, str]:
    """Load mapping of raw column names (line_XXXX) to descriptive names."""
    if not NAMES_DICT_PATH.exists():
        return {}
    df = pl.read_csv(NAMES_DICT_PATH)
    return dict(zip(df["original"], df["descriptive"]))


def scan_rfsd(
    year: int | Sequence[int] | None = None,
    descriptive: bool = False,
    data_dir: Path | str | None = None,
) -> pl.LazyFrame:
    """
    Return a native Polars LazyFrame over RFSD partitioned parquet files.

    Parameters
    ----------
    year : int | Sequence[int] | None
        Specific year (e.g. 2024) or sequence of years to scan.
    descriptive : bool
        If True, renames accounting line items (line_2110 -> PL_revenue, etc.)
    data_dir : Path | str | None
        Optional custom directory containing RFSD partitioned parquet files.
    """
    base_dir = Path(data_dir) if data_dir else RFSD_DIR
    pattern = str(base_dir / "**" / "*.parquet")
    
    lf = pl.scan_parquet(pattern, hive_partitioning=True)
    
    if year is not None:
        if isinstance(year, int) and not isinstance(year, bool):
            lf = lf.filter(pl.col("year") == year)
        elif isinstance(year, Sequence) and not isinstance(year, (str, bytes)):
            if not all(isinstance(y, int) and not isinstance(y, bool) for y in year):
                invalid_types = [type(y).__name__ for y in year if not isinstance(y, int) or isinstance(y, bool)]
                raise TypeError(
                    f"All elements in 'year' sequence must be integers without implicit casting. "
                    f"Found invalid types: {invalid_types}"
                )
            lf = lf.filter(pl.col("year").is_in(list(year)))
        else:
            raise TypeError(
                f"'year' must be an int or Sequence[int] (no implicit casting allowed), "
                f"got {type(year).__name__}: {year!r}"
            )
            
    if descriptive:
        renames = load_renaming_dict()
        if renames:
            schema_cols = set(lf.collect_schema().names())
            valid_renames = {k: v for k, v in renames.items() if k in schema_cols and k != v}
            # Verify no duplicate targets in valid_renames
            seen_targets: set[str] = set()
            duplicate_targets: set[str] = set()
            for target in valid_renames.values():
                if target in seen_targets:
                    duplicate_targets.add(target)
                seen_targets.add(target)
            if duplicate_targets:
                raise ValueError(
                    f"Cannot apply descriptive renames because target column names are duplicated: "
                    f"{sorted(duplicate_targets)}"
                )
            lf = lf.rename(valid_renames)
            
    return lf


def dfo_filter() -> pl.Expr:
    """Return a native Polars expression filtering for DFO companies."""
    return pl.col("region").is_in(DFO_REGIONS) | pl.col("region_taxcode").is_in(DFO_TAXCODES)
