"""uv run python -i explore.py — native Polars, no queries run on import."""

import sys

import polars as pl

from rfsd_regional import dfo_filter, scan_rfsd
from rfsd_regional.analysis import BASE, CLEAN, CLUSTER, GROSS_PROFIT, REVENUE, SECTION

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

lf = scan_rfsd()
dfo = dfo_filter()
base = lf.filter(dfo & BASE)
clean = base.filter(CLEAN)

if __name__ == "__main__":
    print("Ready: pl, lf, dfo, base, clean. Amounts are in thousand RUB.")
