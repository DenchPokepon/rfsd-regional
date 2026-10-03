"""Nominal GRP and GVA from the pinned Rosstat workbooks (billion RUB)."""

import math
import re
from pathlib import Path

import openpyxl
import polars as pl

from .dataset import DFO

CYRILLIC = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H",
                         "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T"})


def number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"Missing or non-numeric Rosstat value: {value!r}")
    return float(value)


def read_grp(path: Path) -> pl.DataFrame:
    records, official = [], {}
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet in ["1", "2"]:
            rows = list(workbook[sheet].values)
            years = {}
            for c, value in enumerate(rows[2]):
                match = re.search(r"(?:19|20)\d{2}", str(value))
                if match and 2011 <= int(match[0]) <= 2024:
                    years[c] = int(match[0])
            for row in rows[3:]:
                name = " ".join(str(row[0] or "").split())
                if name.startswith("Дальневосточный федеральный округ"):
                    official.update({y: number(row[c]) / 1000 for c, y in years.items()})
                for code, (_, label) in DFO.items():
                    if name.startswith(label):
                        records.extend({"year": y, "region_code": code, "grp_bln": number(row[c]) / 1000}
                                       for c, y in years.items())
    finally:
        workbook.close()
    result = pl.DataFrame(records)
    keys = {(r["year"], r["region_code"]) for r in records}
    expected = {(year, code) for year in range(2011, 2025) for code in DFO}
    if keys != expected or len(records) != len(expected):
        raise ValueError("GRP must contain exactly 11 regions x 14 years")
    for year, total in result.group_by("year").agg(pl.col("grp_bln").sum()).iter_rows():
        if year not in official or abs(total - official[year]) > 0.001:
            raise ValueError(f"Regional GRP sum does not match DFO in {year}")
    return result.sort("year", "region_code")


def read_gva(path: Path) -> pl.DataFrame:
    records = []
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        sheets = {s.replace(" ", ""): s for s in workbook.sheetnames}
        for year in range(2016, 2025):
            rows = list(workbook[sheets[f"2.{year}"]].values)
            matches = [row for row in rows if "Дальневосточный федеральный округ" in str(row[0])]
            if len(matches) != 1:
                raise ValueError(f"Expected one DFO row in GVA {year}")
            row = matches[0]
            values = {}
            for c in range(1, len(row)):
                header = " ".join(str(r[c] or "") for r in rows[1:5])
                match = re.search(r"Раздел\s+([A-ZА-Я])", header)
                if match:
                    section = match[1].translate(CYRILLIC)
                    if section in "ABCDEFGHIJKLMNOPQRS":
                        if section in values:
                            raise ValueError(f"Duplicate GVA section {section} in {year}")
                        values[section] = number(row[c]) / 1e6
            if set(values) != set("ABCDEFGHIJKLMNOPQRS"):
                raise ValueError(f"Incomplete GVA sections in {year}")
            if abs(sum(values.values()) - number(row[1]) / 1e6) > 0.001:
                raise ValueError(f"GVA section sum does not match total in {year}")
            records.extend({"year": year, "section": s, "gva_bln": v} for s, v in values.items())
    finally:
        workbook.close()
    return pl.DataFrame(records).sort("year", "section")
